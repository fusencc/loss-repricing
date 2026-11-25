"""调价业务编排。

生产只做会改变金额/对象/是否重复提交的关键核验，不在每次运行重复执行校准测试。
最重要的顺序：刷新输入 -> 规则判定 -> 数据库提交执行意图和商品锁 -> 外部上传 -> 回读。
在上传前后任意时刻崩溃，都把无法确定是否已送达的意图交给核验器；不会自动再发一次。
"""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
import time
from app.observability import event, bind_context, bind_sink, get_sink
from sqlalchemy import func, select, update, or_
from sqlalchemy.exc import IntegrityError
from app.db import transaction
from app.models import Batch, Item, Execution, VariantGuard, Protection, Job
from app.domain.types import Decision, VariantKey, FeeModel, ONLINE_PROFIT_MINIMUM_MARGIN
from app.domain.engine import evaluate, allowed_error, snapshot_problem
from app.domain.money import MoneyRatio
from app.domain.price_reference import saved_price_limit
from app.adapters.base import SourceUnavailable, SubmitResult
from app.adapters.roster_price_upload import ROSTER_UPLOAD_TRACE, VERIFIED_FULFILLMENT, RosterPriceTransport
from app.adapters.official_price_logs import capture_recent_price_changes, recent_price_log_for
from app.adapters.normalization import money
from app.services.store import utcnow, stamp, parse_stamp, BEIJING, settings_value, settings_for_submit, pause_site, enqueue, is_erp_reconciliation, write_decisions
from app.services.coordination import automation_gate, authorization_snapshot, lock_authorization
from app.services.price_control import observe_prices, price_baseline, same_price, record_conflict, lock_control, verified_baseline


class PricingService:
    def __init__(self, factory, adapter, settings):
        self.factory, self.adapter, self.config = factory, adapter, settings
        self._roster_channel = None

    def _roster_transport(self):
        if self._roster_channel is None:
            self._roster_channel = RosterPriceTransport(self.config)
        return self._roster_channel

    def close(self):
        if self._roster_channel is not None:
            self._roster_channel.close()

    def _enrich(self, snapshot):
        """真实近期调价由开放接口证明；本地档案只补充未知执行锁和人工保护。"""
        result = snapshot.model_copy(deep=True)
        official = None
        if self.adapter.mode == 'live':
            official = capture_recent_price_changes(self.config,[row.key for row in result.online],now=utcnow())
            result.source_versions['recent_price_history'] = official['source_id']
        with self.factory() as session:
            protections = session.scalars(select(Protection).where(Protection.active.is_(True))).all()
            for online in result.online:
                guard = session.get(VariantGuard, online.key.target_identity())
                if self.adapter.mode == 'live':
                    online.history_known, online.pending_submission, online.last_increase_at = False, False, None
                    online.recent_price_log = None
                    try:
                        online.recent_price_log = recent_price_log_for(official,online.key)
                        online.history_known = True
                    except (ValueError,TypeError,KeyError):
                        pass  # 该目标保持历史未知，不影响其他有效计数。
                    pending = session.scalar(select(Execution.id).join(Item,Execution.item_id==Item.id)
                        .join(Batch,Item.batch_id==Batch.id).where(Batch.mode=='live',
                            Execution.variant_id==online.key.target_identity(),
                            Execution.state.not_in(['failed','verified'])).limit(1))
                    online.pending_submission = pending is not None
                if guard:
                    online.pending_submission = online.pending_submission or bool(guard.execution_id)
                    effective = parse_stamp(guard.last_effective_at)
                    if self.adapter.mode != "live" and effective and (online.last_increase_at is None or effective > online.last_increase_at):
                        online.last_increase_at = effective
                online.protected = online.protected or any(
                    p.shop_id == online.key.shop_id and p.site == online.key.site and p.main_sku == online.main_sku
                    for p in protections)
        return result

    @staticmethod
    def _runtime_allowed(key, cfg, manual, *, roster=False):
        if cfg["paused"] or key.site in cfg["paused_sites"]:
            return False, "当前范围已暂停新提交"
        if roster:
            return True, ""
        if not manual and not cfg["autonomous_enabled"]:
            return False, "自动运行尚未启用"
        if key.shop_id not in cfg["pilot_shops"] or key.site not in cfg["site_enabled"]:
            return False, "店铺或站点不在已启用试点名单"
        return True, ""

    def _allowed(self, key, manual=False, *, roster=None):
        allowed, reason = self._runtime_allowed(key, settings_value(self.factory), manual, roster=roster is not None)
        if not allowed:
            return allowed, reason
        if roster is not None:
            if (manual or self.adapter.mode != "live" or not self.config.live_submit_enabled
                    or self.config.erp_roster_submit_date != roster.day or self.config.erp_roster_date != roster.day
                    or roster.day != utcnow().astimezone(BEIJING).date()):
                return False, "当日名单真实提交未启用"
            price = roster.prices.get(key.identity())
            if price is None or not price.shop_name:
                return False, "缺少本轮算价与 ERP 店铺的精确绑定"
            if price.fulfillment not in VERIFIED_FULFILLMENT.get(key.site, ()):
                return False, "本站点与仓配组合缺少已验证的单品上传和回读证据"
            if not self._roster_transport().available:
                return False, "单品提交的销售账号或接口地址未配置"
            if self.config.coordination_required:
                with self.factory() as session:
                    return automation_gate(session, key, roster_salesperson=roster.salesperson)
            return True, ""
        if self.adapter.mode == "file":
            return False, "文件模式仅对账，不执行上传"
        if self.adapter.mode == "live":
            readiness = self.adapter.readiness()
            if not self.config.live_submit_enabled or not readiness.get("contract_ready"):
                return False, "真实数据源待接入，提交保持关闭"
            if key.site not in readiness.get("verified_sites", []):
                return False, "本站点尚未完成真实合同验证"
            # 2026-11-17明确单商品人工执行授权：原价基线在run_correction重取核对，
            # 修正权限/目标锁仍由_claim校验，适配器再约束当日目标及精确上传价。
            # 此范围不宣称外部独占，也不允许自动任务借人工授权执行。
            scoped_manual = manual and key.target_identity() in readiness.get('manual_targets', [])
            if self.config.coordination_required and not self.config.live_writer_exclusive and not scoped_manual:
                return False, "外部改价入口及独占写入范围尚未确认，真实提交保持关闭"
        if self.config.coordination_required and not manual:
            with self.factory() as session:
                return automation_gate(session, key)
        return True, ""

    def _save_decisions(self, batch_id, decisions):
        with transaction(self.factory) as session:
            write_decisions(session, batch_id, decisions)

    def run_batch(self, run_key, item_id=None, *, roster=None):
        if roster is not None and (item_id is not None or self.adapter.mode != "live"):
            raise ValueError("固定日期名单仅适用真实批次")
        with bind_sink(get_sink(self.config)), bind_context(operation_id=uuid4().hex):
            event('batch.started', source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))
            started = time.monotonic()
            batch_id = self._run_batch(run_key, item_id, roster=roster)
            event('batch.completed', batch_id=batch_id, source=self.adapter.mode,
                  synthetic=getattr(self.adapter, 'synthetic', False), duration_ms=(time.monotonic() - started) * 1000)
            return batch_id

    def _run_batch(self, run_key, item_id=None, *, roster=None):
        fixed_day = roster.day if roster is not None else None
        approved_prices = roster.prices if roster is not None else None
        target_id = None
        if item_id:
            with self.factory() as session:
                selected = session.get(Item, item_id)
                if selected is None:
                    raise ValueError("待重评记录不存在")
                batch = session.get(Batch, selected.batch_id)
                if batch.mode != self.adapter.mode:
                    raise ValueError("原始批次模式与当前适配器不一致，禁止跨模式重评")
                if is_erp_reconciliation(batch):
                    raise ValueError("文件对账批次仅供查阅")
                target_id = VariantKey.model_validate(selected.key).target_identity()
            # 点选旧记录也核验该稳定目标的全部未知执行，绝不再次上传未知请求。
            self.reconcile(target_id=target_id)
        business_date = utcnow().astimezone(BEIJING).date()
        if fixed_day is not None and business_date != fixed_day:
            raise SourceUnavailable('固定日期批次已跨北京时间业务日')
        # 日界后生成新批次，旧批次里已经送达的意图仍由reconcile独立跟进。
        actual_key = f"{run_key}:{business_date.isoformat()}"
        initial = roster.collect(business_date)[0] if roster is not None else self.adapter.collect(business_date)
        snapshot = self._enrich(initial)
        evaluation_time = utcnow()
        if problem := snapshot_problem(snapshot, evaluation_time, business_date):
            # 不完整的重取不能覆盖先前候选，也不能将未返回的商品标为不调。
            with self.factory() as session:
                previous = session.scalar(select(Batch).where(Batch.run_key == actual_key))
                if previous and previous.mode != self.adapter.mode:
                    raise ValueError("原始批次模式与当前适配器不一致，禁止跨模式运行")
            if previous:
                reason = f"重试取数失败：{problem[1]}，等待后台重新取数"
                self._summarize(previous.id, refresh_error=reason)
                raise SourceUnavailable(reason)
            if self.adapter.mode == "live":
                # 首次真实取数未就绪，保持当日任务可重试，不建空批次。
                raise SourceUnavailable(problem[1])
        authorizations = {}
        # 跨日判定仅供查看，不将上一日观察用于接管或外部改价冲突判断。
        if self.config.coordination_required and evaluation_time.astimezone(BEIJING).date() == business_date:
            observed = snapshot
            if approved_prices is not None:
                observed = snapshot.model_copy(update={
                    "online": [row for row in snapshot.online if row.key.identity() in approved_prices],
                })
            authorizations = observe_prices(self.factory, observed)
        decisions = evaluate(snapshot, evaluation_time)
        if self.config.coordination_required:
            with self.factory() as session:
                for decision in decisions:
                    decision.trace['authorization']=authorizations.get(decision.key.target_identity(),{})
        if target_id:
            decisions = [d for d in decisions if d.key.target_identity() == target_id]
        with transaction(self.factory) as session:
            batch = session.scalar(select(Batch).where(Batch.run_key == actual_key))
            if not batch:
                archive = snapshot.model_dump(mode="json")
                if approved_prices is not None:
                    archive["erp_roster_approved_prices"] = {
                        key: price.as_json() for key, price in approved_prices.items()
                    }
                batch = Batch(id=str(uuid4()), run_key=actual_key, business_date=business_date.isoformat(),
                    mode=self.adapter.mode, status="running", created_at=stamp(), snapshot=archive, summary={})
                session.add(batch)
            else:
                # 模式属于原始批次证据，不能靠切换适配器覆盖成另一模式。
                if batch.mode != self.adapter.mode:
                    raise ValueError("原始批次模式与当前适配器不一致，禁止跨模式运行")
                # 名单任务重启后，首次入批的商品及精确拟价继续限定授权范围。
                if approved_prices is not None:
                    original_prices = batch.snapshot["erp_roster_approved_prices"]
                    approved_prices = {
                        key: price for key, price in approved_prices.items()
                        if price.matches_saved(original_prices.get(key))
                    }
                batch.status, batch.error = "running", None
            batch_id = batch.id
        if approved_prices is not None:
            decisions = [d for d in decisions if d.key.identity() in approved_prices]
        self._save_decisions(batch_id, decisions)
        event('batch.saved', batch_id=batch_id, count=len(decisions),
              source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))
        if utcnow().astimezone(BEIJING).date() != business_date:
            self._rollover(run_key, batch_id, item_id, fixed_day=fixed_day)
            self._summarize(batch_id)
            return batch_id

        # 对整批做一次紧邻提交的刷新；不会为每个SKU重跑四份ERP全量导出。
        # 若执行耗时超过5分钟，再刷新，保证关键在线信息足够新。
        fresh_at, fresh_decisions, fresh_approved = None, {}, {}
        with self.factory() as session:
            item_ids = session.scalars(select(Item.id).where(Item.batch_id == batch_id, Item.phase == "proposed")).all()
        selected_item_id = item_id
        for item_id in item_ids:
            if utcnow().astimezone(BEIJING).date() != business_date:
                self._rollover(run_key, batch_id, selected_item_id, fixed_day=fixed_day)
                break
            with self.factory() as session:
                item = session.get(Item, item_id)
                original = Decision.model_validate(item.decision)
            price = None
            if approved_prices is not None:
                price = approved_prices.get(original.key.identity())
                if price is None or (price.upload_price, price.target_actual) != (original.upload_price, original.target_actual):
                    # 租约恢复后的旧待提交记录也必须在本轮通过的名单、店铺和金额内。
                    self._hold(item_id, "记录已超出本轮有效算价范围")
                    continue
            allowed, reason = self._allowed(original.key, roster=roster)
            if not allowed:
                self._hold(item_id, reason)
                continue
            if fresh_at is None or (utcnow() - fresh_at).total_seconds() >= 300:
                try:
                    if roster is not None:
                        latest, fresh_approved = roster.collect(business_date)
                    else:
                        latest = self.adapter.collect(business_date)
                    refreshed = self._enrich(latest)
                except SourceUnavailable:
                    if utcnow().astimezone(BEIJING).date() != business_date:
                        self._rollover(run_key, batch_id, selected_item_id, fixed_day=fixed_day)
                        break
                    self._summarize(batch_id, refresh_error="提交前刷新失败，等待后台重新取数")
                    raise
                if utcnow().astimezone(BEIJING).date() != business_date:
                    self._rollover(run_key, batch_id, selected_item_id, fixed_day=fixed_day)
                    break
                evaluation_time = utcnow()
                # 空快照也必须证明四类来源齐备。否则缺行不能解释成候选消失。
                if problem := snapshot_problem(refreshed, evaluation_time, business_date):
                    reason = f"提交前刷新失败：{problem[1]}，等待后台重新取数"
                    self._summarize(batch_id, refresh_error=reason)
                    raise SourceUnavailable(reason)
                fresh_decisions = {d.key.identity(): d for d in evaluate(refreshed, evaluation_time)}
                fresh_at = utcnow()
            decision = fresh_decisions.get(original.key.identity())
            if decision is None:
                self._hold(item_id, "提交前刷新后候选已消失")
                continue
            if self.config.coordination_required:
                decision.trace['authorization']=original.trace.get('authorization')
                before = original.trace.get("online_rows", [])
                after = decision.trace.get("online_rows", [])
                if (len(before) == 1 and len(after) == 1
                        and not same_price(price_baseline(before[0]), price_baseline(after[0]))):
                    reason = "提交前检测到外部价格、折扣、仓配或版本变化，原方案已失效，等待负责人核对"
                    record_conflict(self.factory, original.key, reason)
                    self._hold(item_id, reason)
                    continue
            with transaction(self.factory) as session:
                item = session.get(Item, item_id)
                item.decision = decision.model_dump(mode="json")
                item.status, item.reason = decision.status, decision.reason
            if not decision.ready:
                self._hold(item_id, decision.reason, decision.status)
                continue
            if price is not None and (
                    decision.upload_price != original.upload_price or decision.target_actual != original.target_actual
                    or fresh_approved.get(decision.key.identity()) != price):
                self._hold(item_id, "提交前重算结果已变化，原批次价格或商品归属待重新核对")
                continue
            if self.adapter.mode == "live" and not (roster is not None and roster.reuse_archived_inputs):
                online = next((o for o in refreshed.online if o.key.identity() == decision.key.identity()), None)
                if online is None or (utcnow() - online.observed_at).total_seconds() > 300:
                    self._hold(item_id, "当前在线价格快照超过5分钟，等待新的有效价数据")
                    continue
            if self._submit(item_id, decision, business_date, roster=roster) is False:
                self._rollover(run_key, batch_id, selected_item_id, fixed_day=fixed_day)
                break
            # 已完成当前单条后立即核验，发生价格超差能及时阻止本站点后续上传。
            self.reconcile(item_id=item_id, summarize_batch=False)
        else:
            # 最后一个候选可能在计算/挂起阶段跨日，没有下一轮循环替它触发重新取数。
            if utcnow().astimezone(BEIJING).date() != business_date:
                self._rollover(run_key, batch_id, selected_item_id, fixed_day=fixed_day)
        self._summarize(batch_id)
        return batch_id

    def _rollover(self, run_key, batch_id, selected_item_id, *, fixed_day=None):
        """跨北京时间日界后丢弃缓存；持久排队新日期评估，保留单目标授权范围。"""
        # 单日名单跨日只结束原范围，避免转成次日全量任务。
        if fixed_day is None:
            today = utcnow().astimezone(BEIJING).date().isoformat()
            with self.factory() as session:
                job = session.get(Job, run_key)
                scheduled_day = job.payload.get("date") if job else None
                scheduled = job and job.dedupe_key == f"daily:{scheduled_day}" and scheduled_day
            if scheduled and not selected_item_id:
                # 定时批次的跨日重算与新日调度共用日唯一键。
                enqueue(self.factory, "run", {"date": today}, f"daily:{today}")
            else:
                enqueue(self.factory, "run", {"item_id": selected_item_id} if selected_item_id else {},
                        f"rollover:{batch_id}:{today}")
        reason = ('固定名单批次已跨北京时间业务日' if fixed_day is not None else
                  '已跨北京时间业务日，已排队重新取数计算')
        with transaction(self.factory) as session:
            for item in session.scalars(select(Item).where(Item.batch_id == batch_id, Item.phase == "proposed")):
                item.status, item.phase = "待处理", "blocked"
                item.reason, item.updated_at = reason, stamp()

    def _hold(self, item_id, reason, status="待处理", *, decision=None):
        with transaction(self.factory) as session:
            item = session.get(Item, item_id)
            item.status, item.phase, item.reason, item.updated_at = status, "blocked", reason, stamp()
            if decision is not None:
                item.decision = decision.model_dump(mode="json")
        event('execution.blocked', item_id=item_id)

    def _locked_submission_access(self, session, decision, roster):
        control = lock_control(session, decision.key)
        correction_id = decision.trace.get('correction_id')
        if correction_id:
            from app.models.coordination import Correction
            from app.services.corrections import correction_gate
            correction = session.get(Correction, correction_id)
            users = [correction.requested_by, correction.approved_by] if correction else []
            locked = lock_authorization(session, decision.key, users)
            return correction_gate(session, decision, control, locked=locked)
        locked = lock_authorization(session, decision.key)
        return automation_gate(
            session, decision.key, decision.trace.get("authorization", {}), locked=locked,
            roster_salesperson=roster.salesperson if roster is not None else None,
        )

    def _claim(self, item_id, decision, *, roster=None, execution_id=None):
        identity = decision.key.target_identity()
        # 先确保锁行存在；并发创建的冲突只需重新读取，不影响已有执行。
        try:
            with transaction(self.factory) as session:
                if session.get(VariantGuard, identity) is None:
                    session.add(VariantGuard(variant_id=identity))
        except IntegrityError:
            pass
        execution_id = execution_id or str(uuid4())
        with transaction(self.factory) as session:
            if session.scalar(select(Execution.id).where(Execution.item_id == item_id)):
                return None
            # 提交前新增的历史未知告警也须进入商品证据，供独立机器人 outbox 读取。
            item = session.get(Item, item_id)
            item.decision = decision.model_dump(mode="json")
            if self.config.coordination_required:
                allowed, reason = self._locked_submission_access(session, decision, roster)
                if not allowed:
                    item = session.get(Item, item_id)
                    item.status, item.phase, item.reason, item.updated_at = "待处理", "blocked", reason, stamp()
                    return None
            # 依据已确认设计“特殊人工保护”：外部快照可批内复用，本地保护不能等缓存过期。
            # 主SKU只取纯计算留下的同一在线身份证据，不从子SKU或商品ID猜测。
            # 在领取意图的事务内读取已提交的保护变更，仅阻断当前目标，不重新导出整批。
            item = session.get(Item, item_id)
            rows = decision.trace.get("online_rows", [])
            key = decision.key.model_dump(mode="json")
            if (not isinstance(rows, list) or not rows
                    or any(not isinstance(row, dict) or row.get("key") != key
                           or not isinstance(row.get("main_sku"), str) or not row["main_sku"].strip()
                           for row in rows)
                    or len({row["main_sku"] for row in rows}) != 1):
                item.status, item.phase = "待处理", "blocked"
                item.reason, item.updated_at = "缺少一致的在线主SKU证据，无法核对提交前人工保护", stamp()
                return None
            protection = session.scalar(select(Protection).where(
                Protection.active.is_(True), Protection.shop_id == decision.key.shop_id,
                Protection.site == decision.key.site, Protection.main_sku == rows[0]["main_sku"]
            ).limit(1))
            if protection:
                item.status, item.phase = "不调价", "excluded"
                item.reason = "提交前命中人工保护名单：" + protection.reason
                item.updated_at = stamp()
                return None
            cutoff = stamp(utcnow() - timedelta(hours=72))
            conditions = [VariantGuard.variant_id == identity, VariantGuard.execution_id.is_(None)]
            if self.adapter.mode != "live" and not decision.trace.get("correction_id"):
                conditions.append(or_(VariantGuard.last_effective_at.is_(None), VariantGuard.last_effective_at <= cutoff))
            claimed = session.execute(update(VariantGuard).where(*conditions).values(execution_id=execution_id))
            if claimed.rowcount != 1:
                item = session.get(Item, item_id)
                item.status, item.phase, item.reason = "待核验", "blocked", "同一在线变体仍在核验或距生效不足72小时"
                return None
            session.add(Execution(id=execution_id, item_id=item_id, variant_id=identity, state="submitting",
                request=decision.model_dump(mode="json"), response={}, evidence={}, created_at=stamp(), updated_at=stamp()))
            if decision.trace.get("correction_id"):
                from app.models.coordination import Correction
                correction = session.get(Correction, decision.trace["correction_id"])
                correction.execution_id, correction.state = execution_id, "executing"
            item = session.get(Item, item_id)
            item.status, item.phase, item.updated_at = "待核验", "submitting", stamp()
            item.reason = "执行意图已持久保存，等待逐行结果与实际价格"
        # 事务退出后才能记录“已持久保存”，日志不冒充数据库提交成功。
        event('execution.intent_saved', execution_id=execution_id, item_id=item_id, batch_id=item.batch_id,
              source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))
        return execution_id

    def _submit(self, item_id, decision, business_date=None, *, roster=None):
        if business_date and utcnow().astimezone(BEIJING).date() != business_date:
            return False
        manual = bool(decision.trace.get("correction_id"))
        allowed, reason = self._allowed(decision.key, manual=manual, roster=roster)
        if not allowed:
            self._hold(item_id, reason)
            return
        if self.adapter.mode == 'live' and not manual:
            # 批内可复用算价输入，但上传前重查该子SKU，捕获其他入口刚产生的日志。
            official = capture_recent_price_changes(self.config,[decision.key],now=utcnow())
            decision.trace['official_price_history_before_submit'] = official
            try:
                recent = recent_price_log_for(official,decision.key)
            except (ValueError,TypeError,KeyError):
                recent = None
                decision.trace.setdefault('warning_codes', []).append('unknown_history')
                decision.trace.setdefault('warnings', []).append('提交前近72小时调价历史无法确认，仅告警')
            if recent is not None and recent.count > 0:
                decision.ready, decision.status = False, '不调价'
                decision.reason_code, decision.reason = 'recent_price_change', '提交前查到近72小时调价日志，不调价'
                self._hold(item_id,decision.reason,decision.status,decision=decision)
                return
        if roster is not None:
            price = roster.prices[decision.key.identity()]
            with self.factory() as session:
                item = session.get(Item, item_id)
                batch = session.get(Batch, item.batch_id)
            if (
                    batch.mode != "live" or batch.business_date != roster.day.isoformat()
                    or batch.snapshot.get("source_versions", {}).get("erp_roster_salesperson") != roster.salesperson
                    or not price.matches_saved(batch.snapshot.get("erp_roster_approved_prices", {}).get(decision.key.identity()))
                    or (price.upload_price, price.target_actual) != (decision.upload_price, decision.target_actual)):
                self._hold(item_id, "确认的名单、店铺或金额与本轮执行不一致")
                return
            execution_id = str(uuid4())
            try:
                decision.trace[ROSTER_UPLOAD_TRACE] = self._roster_transport().prepare(
                    execution_id, batch.id, roster.day, price.shop_name, decision,
                )
            except (SourceUnavailable, ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
                error_id = event('execution.roster_preflight_failed', exception=exc, item_id=item_id)
                reason = str(exc) if type(exc) in (SourceUnavailable, ValueError) else "单品提交前证据不完整"
                self._hold(item_id, reason + (f"（排查编号 {error_id}）" if error_id else ""))
                return
            execution_id = self._claim(item_id, decision, roster=roster, execution_id=execution_id)
        else:
            execution_id = self._claim(item_id, decision)
        if not execution_id:
            return
        # 执行意图已在上个事务持久化。最后一段与人工接管串行化：
        # 请求已开始后接管不会撤销外部调用；崩溃时上个意图仍在，继续核验不重传。
        with transaction(self.factory) as session:
            if self.config.coordination_required:
                allowed, reason = self._locked_submission_access(session, decision, roster)
            # 执行意图落库后，管理员或本站点超差仍可能撤回新提交授权。
            # 在最后一次外部调用前读取当前设置，只有能证明尚未调用才可释放本次锁。
            if allowed:
                allowed, reason = self._runtime_allowed(
                    decision.key, settings_for_submit(session), manual, roster=roster is not None,
                )
            # 等待授权或设置锁也可能跨日。以外部调用前的业务日为准。
            now = utcnow()
            crossed_day = business_date is not None and now.astimezone(BEIJING).date() != business_date
            if allowed and not crossed_day and self.adapter.mode == "live":
                # 归档续跑的实时价格证据由单品预检独立提供，归档本身不冒充现态。
                if roster is not None and roster.reuse_archived_inputs:
                    observation = decision.trace[ROSTER_UPLOAD_TRACE]
                else:
                    observation = decision.trace["online_rows"][0]
                observed_at = parse_stamp(observation.get("observed_at"))
                if observed_at is None or not timedelta(0) <= now - observed_at <= timedelta(minutes=5):
                    allowed, reason = False, "当前在线价格快照无效或超过5分钟，等待新的有效价数据"
            if crossed_day or not allowed:
                if crossed_day:
                    reason = "已跨北京时间业务日，等待重新取数计算"
                execution, item = session.get(Execution, execution_id), session.get(Item, item_id)
                execution.state, execution.updated_at = "failed", stamp()
                execution.response = {"status": "not_submitted", "message": reason}
                guard = session.get(VariantGuard, execution.variant_id)
                if guard.execution_id == execution_id:
                    guard.execution_id = None
                item.status, item.phase, item.reason, item.updated_at = "待处理", "blocked", reason, stamp()
                return False if crossed_day else None
            try:
                event('execution.submit_started', execution_id=execution_id, item_id=item_id,
                      source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))
                if roster is not None:
                    result = self._roster_transport().submit(execution_id, decision)
                else:
                    result = self.adapter.submit(execution_id, decision)
            except Exception as exc:
                event('execution.unknown', exception=exc, execution_id=execution_id, item_id=item_id,
                      source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))
                result = SubmitResult("unknown", message=f"提交结果未知（{type(exc).__name__}），仅查结果、不重传")
            execution = session.get(Execution, execution_id)
            item = session.get(Item, item_id)
            execution.task_id, execution.state = result.task_id, "failed" if result.status == "rejected" else result.status
            execution.response = {"status": result.status, "message": result.message}
            execution.updated_at = stamp()
            item.phase = execution.state
            item.reason = result.message or "上传已受理，等待实际价格回读"
            if result.status == "rejected":
                item.status = "待处理"
                session.get(VariantGuard, execution.variant_id).execution_id = None
            else:
                item.status = "待核验"
            item.updated_at = stamp()
        event('execution.result_saved', execution_id=execution_id, item_id=item_id, batch_id=item.batch_id,
              state=execution.state, source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))

    def reconcile(self, item_id=None, target_id=None, *, summarize_batch=True):
        # 核验不受暂停开关限制；否则暂停后已送达的请求将永久失去跟踪。
        with self.factory() as session:
            # 依据模式隔离约束：只让原始批次同模式的适配器回读；其他模式的状态和锁原样保留。
            query = (select(Execution).join(Item, Execution.item_id == Item.id)
                     .join(Batch, Item.batch_id == Batch.id)
                     .where(Batch.mode == self.adapter.mode,
                            Execution.state.in_(["submitting", "accepted", "unknown", "mismatch"])))
            if item_id:
                query = query.where(Execution.item_id == item_id)
            if target_id:
                query = query.where(Execution.variant_id == target_id)
            pending = session.scalars(query).all()
        for execution in pending:
            try:
                # 请求证据损坏时保留本条锁，其他商品仍继续回查。
                decision = Decision.model_validate(execution.request)
                if decision.key.target_identity() != execution.variant_id:
                    raise ValueError("保存的调价请求与执行锁目标不一致")
                if self.adapter.mode == "live" and ROSTER_UPLOAD_TRACE in decision.trace:
                    result = self._roster_transport().lookup(execution.id, execution.task_id, decision)
                else:
                    result = self.adapter.lookup(execution.id, execution.task_id, decision.key)
                if result.price_record is not None:
                    from app.services.price_log_archive import validate_price_record_binding
                    validate_price_record_binding(result.price_record, execution.id, decision.key,
                        decision.upload_price, result.actual_price)
                difference_ratio = None
                if (result.status == "accepted" and result.actual_price is not None
                        and result.actual_price.is_finite() and result.actual_price > 0):
                    # 有精确比值就校验并使用它；旧记录仅按持久目标价核验。
                    if decision.target_actual is None or decision.target_actual <= 0:
                        raise ValueError("保存的调价目标价缺失或无效")
                    if "exact_target_actual" in decision.trace:
                        exact = decision.trace["exact_target_actual"]
                        target = MoneyRatio(Decimal(exact["numerator"]), Decimal(exact["denominator"]))
                    else:
                        target = MoneyRatio(decision.target_actual)
                    if target.numerator <= 0:
                        raise ValueError("保存的精确目标价无效")
                    difference_ratio = (MoneyRatio(result.actual_price) - target).absolute()
                    difference = difference_ratio.decimal()
                    # 按保存请求判定：有参考时优先上限，无参考时仍要求1%利润。
                    rows = decision.trace["online_rows"]
                    if len(rows) != 1:
                        raise ValueError("保存的在线利润模型来源不唯一")
                    model = FeeModel.model_validate(rows[0]["profit_check"]["model"])
                    margin_passed = model.evaluate(MoneyRatio(result.actual_price)).meets_minimum_margin
                    upper = saved_price_limit(decision.trace)
                    minimum_margin_required = upper is None
                    price_limit_passed = upper is None or MoneyRatio(result.actual_price) <= upper
            except Exception as exc:
                event('execution.lookup_failed', exception=exc, execution_id=execution.id,
                      item_id=execution.item_id, source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))
                continue  # 稍后继续核验，保持原锁，不把查询失败当作未提交。
            with transaction(self.factory) as session:
                control = lock_control(session, decision.key) if self.config.coordination_required else None
                # 迁移前同一目标可能已有多个未知执行；锁行串行化全部历史执行的释放。
                guard = session.scalar(select(VariantGuard).where(
                    VariantGuard.variant_id == execution.variant_id).with_for_update())
                current = session.get(Execution, execution.id, with_for_update=True)
                if current.state not in {"submitting", "accepted", "unknown", "mismatch"}:
                    continue
                last_observed_at = parse_stamp(current.evidence.get("observed_at"))
                if (result.status == "accepted" and last_observed_at is not None
                        and result.observed_at is not None and result.observed_at.utcoffset() is not None
                        and result.observed_at <= last_observed_at):
                    # 超差后须有更新的价格观测，晚到的旧快照或同一时点的冲突值不能解锁。
                    continue
                item = session.get(Item, current.item_id)
                if result.status == "rejected":
                    current.state, item.phase, item.status = "failed", "failed", "待处理"
                    current.evidence = {"row_status": "rejected", "message": result.message}
                    item.reason = result.message or "逐行处理已确认失败，需重新评估"
                elif (difference_ratio is not None and result.price_basis == "actual_including_vat" and result.observed_at is not None
                      and result.observed_at.tzinfo is not None
                      and parse_stamp(current.created_at) <= result.observed_at <= utcnow() + timedelta(seconds=30)):
                    current.evidence = {"actual_price": str(result.actual_price), "target_actual": str(decision.target_actual),
                        "difference": str(difference), "tolerance": str(allowed_error(decision.key.site)),
                        "observed_at": stamp(result.observed_at), "price_basis": result.price_basis,
                        "message": result.message, "mode": self.adapter.mode,
                        "minimum_margin": str(ONLINE_PROFIT_MINIMUM_MARGIN), "online_margin_passed": margin_passed,
                        "minimum_margin_required": minimum_margin_required}
                    if upper is not None:
                        current.evidence.update(price_limit_passed=price_limit_passed,
                            reference_upper_actual=str(upper.decimal()))
                    if result.price_record is not None:
                        current.evidence['price_record'] = result.price_record
                    if (difference_ratio <= MoneyRatio(allowed_error(decision.key.site))
                            and (not minimum_margin_required or margin_passed) and price_limit_passed):
                        confirmed_at = utcnow()
                        effective = result.effective_at
                        effective_time_source = "effective_at"
                        if (effective is None or effective.tzinfo is None or effective < parse_stamp(current.created_at)
                                or effective > result.observed_at):
                            # 依据已确认设计：缺少有效精确生效时间，72小时从首次成功确认时刻起算。
                            # observed_at仅是来源观察时间，旧观察不能替代本程序的首次确认；
                            # 回读新鲜度由真实适配器按已确认的max_age_seconds合同检查，
                            # 服务层不再另猜一个统一5分钟门槛。
                            effective = confirmed_at
                            effective_time_source = "confirmed_at"
                        current.evidence["confirmed_at"] = stamp(confirmed_at)
                        current.evidence["effective_at"] = stamp(effective)
                        current.evidence["effective_time_source"] = effective_time_source
                        current.state, item.phase, item.status = "verified", "verified", "已核验"
                        item.reason = "当前实际有效售价已逐条核验"
                        # 较旧请求晚核验不得倒退最近生效时间。
                        guard.last_effective_at = max(filter(None, [guard.last_effective_at, stamp(effective)]))
                        if control is not None:
                            verified_baseline(control, decision, result.actual_price)
                    else:
                        current.state, item.phase, item.status = "mismatch", "mismatch", "待核验"
                        item.reason = ("回读营销价超过同SKU参考上限，已暂停本站点新提交" if not price_limit_passed else
                            "实际售价与目标超出误差范围，已暂停本站点新提交" if not minimum_margin_required or margin_passed else
                            "回读在线利润率未达到1%，已暂停本站点新提交")
                else:
                    # 未获得新业务证据，不刷新商品与执行的更新时间，也不重复生成批次摘要。
                    if current.state == "mismatch":
                        pause_site(session, decision.key.site)
                    continue
                current.updated_at = item.updated_at = stamp()
                if decision.trace.get("correction_id"):
                    from app.models.coordination import Correction
                    correction = session.get(Correction, decision.trace["correction_id"])
                    if correction:
                        correction.state = {"verified": "verified", "failed": "failed"}.get(current.state, "executing")
                        correction.updated_at, correction.result = stamp(), dict(current.evidence)
                session.flush()
                # 只有所有历史未知/超差执行都终结后才释放；代表锁指针不会遗忘其他请求。
                guard.execution_id = session.scalar(select(Execution.id).where(
                    Execution.variant_id == current.variant_id,
                    Execution.state.in_(["submitting", "accepted", "unknown", "mismatch"])
                ).order_by(Execution.created_at, Execution.id).limit(1))
                if current.state == "mismatch":
                    # 超差证据、商品待核验和站点暂停一起提交，避免崩溃留下已超差但仍可新发的站点。
                    pause_site(session, decision.key.site)
                if summarize_batch:
                    # 后台回查与批次、通知同事务。批次内逐条回查由运行入口收尾。
                    self._write_batch_summary(session, item.batch_id)
            if current.state in {'verified', 'failed', 'mismatch'}:
                event({'verified': 'execution.verified', 'failed': 'execution.rejected',
                       'mismatch': 'execution.mismatch'}[current.state],
                      execution_id=execution.id, item_id=execution.item_id, batch_id=item.batch_id,
                      state=current.state, source=self.adapter.mode, synthetic=getattr(self.adapter, 'synthetic', False))

    def _summarize(self, batch_id, *, refresh_error=None):
        with transaction(self.factory) as session:
            batch = session.get(Batch, batch_id)
            if refresh_error:
                # 仅挂起尚未发送的候选，旧执行及目标锁仍由核验器负责。
                session.execute(update(Item).where(Item.batch_id == batch_id, Item.phase == "proposed").values(
                    phase="blocked", status="待处理", reason=refresh_error, updated_at=stamp(),
                ))
                batch.error = refresh_error
            self._write_batch_summary(session, batch_id)

    def _write_batch_summary(self, session, batch_id):
        batch = session.get(Batch, batch_id)
        counts = dict(session.execute(
            select(Item.status, func.count()).where(Item.batch_id == batch_id).group_by(Item.status)
        ).all())
        failed = bool(batch.error)
        status = "failed" if failed else "pending_verification" if counts.get("待核验") else "completed"
        session.execute(update(Batch).where(Batch.id == batch_id).values(summary=counts, status=status))
        if self.config.coordination_required:
            # 批次结果和待发通知一起提交。真正投递由独立 worker 读取通知表。
            from app.services.notifications import write_batch_notices
            write_batch_notices(session, batch_id, existing_only=failed)
