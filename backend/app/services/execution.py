"""调价业务编排。

生产只做会改变金额/对象/是否重复提交的关键核验，不在每次运行重复执行校准测试。
最重要的顺序：刷新输入 -> 规则判定 -> 数据库提交执行意图和商品锁 -> 外部上传 -> 回读。
在上传前后任意时刻崩溃，都把无法确定是否已送达的意图交给核验器；不会自动再发一次。
"""
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from app.db import transaction
from app.models import Batch, Item, Execution, VariantGuard, Protection
from app.domain.types import Decision, VariantKey
from app.domain.engine import evaluate, allowed_error
from app.domain.money import MoneyRatio
from app.adapters.base import SubmitResult
from app.services.store import utcnow, stamp, parse_stamp, BEIJING, settings_value, update_settings, enqueue
from app.services.coordination import automation_gate, authorization_snapshot, lock_authorization
from app.services.price_control import observe_prices, price_baseline, same_price, record_conflict, lock_control, verified_baseline


class PricingService:
    def __init__(self, factory, adapter, settings):
        self.factory, self.adapter, self.config = factory, adapter, settings

    def _enrich(self, snapshot):
        """外部历史和本程序历史合并；外部历史读取失败仍保持unknown，不用本地记录掩盖。"""
        result = snapshot.model_copy(deep=True)
        with self.factory() as session:
            protections = session.scalars(select(Protection).where(Protection.active.is_(True))).all()
            for online in result.online:
                guard = session.get(VariantGuard, online.key.target_identity())
                if guard:
                    online.pending_submission = online.pending_submission or bool(guard.execution_id)
                    effective = parse_stamp(guard.last_effective_at)
                    if effective and (online.last_increase_at is None or effective > online.last_increase_at):
                        online.last_increase_at = effective
                online.protected = online.protected or any(
                    p.shop_id == online.key.shop_id and p.site == online.key.site and p.main_sku == online.main_sku
                    for p in protections)
        return result

    def _allowed(self, key, manual=False):
        cfg = settings_value(self.factory)
        if cfg["paused"] or key.site in cfg["paused_sites"]:
            return False, "当前范围已暂停新提交"
        if not manual and not cfg["autonomous_enabled"]:
            return False, "自动运行尚未启用"
        if key.shop_id not in cfg["pilot_shops"] or key.site not in cfg["site_enabled"]:
            return False, "店铺或站点不在已启用试点名单"
        if self.adapter.mode == "file":
            return False, "文件模式仅对账，不执行上传"
        if self.adapter.mode == "live":
            readiness = self.adapter.readiness()
            if not self.config.live_submit_enabled or not readiness.get("contract_ready"):
                return False, "真实数据源待接入，提交保持关闭"
            if key.site not in readiness.get("verified_sites", []):
                return False, "本站点尚未完成真实合同验证"
            if self.config.coordination_required and not self.config.live_writer_exclusive:
                return False, "外部改价入口及独占写入范围尚未确认，真实提交保持关闭"
        if self.config.coordination_required and not manual:
            with self.factory() as session:
                return automation_gate(session, key)
        return True, ""

    def _save_decisions(self, batch_id, decisions):
        with transaction(self.factory) as session:
            existing = {row.variant_id: row for row in session.scalars(select(Item).where(Item.batch_id == batch_id))}
            seen = set()
            for decision in decisions:
                identity = decision.key.identity()
                seen.add(identity)
                item = existing.get(identity)
                if item and item.phase in {"submitting", "accepted", "unknown", "verified", "mismatch", "failed"}:
                    continue
                if item is None:
                    item = Item(id=str(uuid4()), batch_id=batch_id, variant_id=identity,
                                key=decision.key.model_dump(mode="json"))
                    session.add(item)
                item.decision = decision.model_dump(mode="json")
                item.status, item.reason = decision.status, decision.reason
                item.phase = "proposed" if decision.ready else "excluded" if decision.status == "本轮不调" else "blocked"
                item.updated_at = stamp()
            for identity, item in existing.items():
                if identity not in seen and item.phase in {"proposed", "blocked", "excluded"}:
                    item.status, item.phase, item.reason = "本轮不调", "excluded", "重新取数后已不在当前亏损候选中"
                    item.updated_at = stamp()

    def run_batch(self, run_key, item_id=None):
        target_id = None
        if item_id:
            with self.factory() as session:
                selected = session.get(Item, item_id)
                if selected is None:
                    raise ValueError("待重评记录不存在")
                if session.get(Batch, selected.batch_id).mode != self.adapter.mode:
                    raise ValueError("原始批次模式与当前适配器不一致，禁止跨模式重评")
                target_id = VariantKey.model_validate(selected.key).target_identity()
            # 点选旧记录也核验该稳定目标的全部未知执行，绝不再次上传未知请求。
            self.reconcile(target_id=target_id)
        business_date = utcnow().astimezone(BEIJING).date()
        # 日界后生成新批次，旧批次里已经送达的意图仍由reconcile独立跟进。
        actual_key = f"{run_key}:{business_date.isoformat()}"
        snapshot = self._enrich(self.adapter.collect(business_date))
        authorizations={}
        if self.config.coordination_required:
            authorizations=observe_prices(self.factory, snapshot)
        decisions = evaluate(snapshot, utcnow())
        if self.config.coordination_required:
            with self.factory() as session:
                for decision in decisions:
                    decision.trace['authorization']=authorizations.get(decision.key.target_identity(),{})
        if target_id:
            decisions = [d for d in decisions if d.key.target_identity() == target_id]
        with transaction(self.factory) as session:
            batch = session.scalar(select(Batch).where(Batch.run_key == actual_key))
            if not batch:
                batch = Batch(id=str(uuid4()), run_key=actual_key, business_date=business_date.isoformat(),
                    mode=self.adapter.mode, status="running", created_at=stamp(),
                    snapshot=snapshot.model_dump(mode="json"), summary={})
                session.add(batch)
            else:
                # 模式属于原始批次证据，不能靠切换适配器覆盖成另一模式。
                if batch.mode != self.adapter.mode:
                    raise ValueError("原始批次模式与当前适配器不一致，禁止跨模式运行")
                batch.snapshot = snapshot.model_dump(mode="json")
                batch.status = "running"
            batch_id = batch.id
        self._save_decisions(batch_id, decisions)
        if utcnow().astimezone(BEIJING).date() != business_date:
            self._rollover(batch_id, item_id)
            self._summarize(batch_id)
            return batch_id

        # 对整批做一次紧邻提交的刷新；不会为每个SKU重跑四份ERP全量导出。
        # 若执行耗时超过5分钟，再刷新，保证关键在线信息足够新。
        fresh_at, fresh_decisions = None, {}
        with self.factory() as session:
            item_ids = session.scalars(select(Item.id).where(Item.batch_id == batch_id, Item.phase == "proposed")).all()
        selected_item_id = item_id
        for item_id in item_ids:
            if utcnow().astimezone(BEIJING).date() != business_date:
                self._rollover(batch_id, selected_item_id)
                break
            with self.factory() as session:
                item = session.get(Item, item_id)
                original = Decision.model_validate(item.decision)
            allowed, reason = self._allowed(original.key)
            if not allowed:
                self._hold(item_id, reason)
                continue
            if fresh_at is None or (utcnow() - fresh_at).total_seconds() >= 300:
                refreshed = self._enrich(self.adapter.collect(business_date))
                if utcnow().astimezone(BEIJING).date() != business_date:
                    self._rollover(batch_id, selected_item_id)
                    break
                fresh_decisions = {d.key.identity(): d for d in evaluate(refreshed, utcnow())}
                fresh_at = utcnow()
                with transaction(self.factory) as session:
                    session.get(Batch, batch_id).snapshot = refreshed.model_dump(mode="json")
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
            if self.adapter.mode == "live":
                online = next((o for o in refreshed.online if o.key.identity() == decision.key.identity()), None)
                if online is None or (utcnow() - online.observed_at).total_seconds() > 300:
                    self._hold(item_id, "当前在线价格快照超过5分钟，等待新的有效价数据")
                    continue
            if self._submit(item_id, decision, business_date) is False:
                self._rollover(batch_id, selected_item_id)
                break
            # 已完成当前单条后立即核验，发生价格超差能及时阻止本站点后续上传。
            self.reconcile(item_id=item_id)
        else:
            # 最后一个候选可能在计算/挂起阶段跨日，没有下一轮循环替它触发重新取数。
            if utcnow().astimezone(BEIJING).date() != business_date:
                self._rollover(batch_id, selected_item_id)
        self._summarize(batch_id)
        return batch_id

    def _rollover(self, batch_id, selected_item_id):
        """跨北京时间日界后丢弃缓存；持久排队新日期评估，保留单目标授权范围。"""
        today = utcnow().astimezone(BEIJING).date().isoformat()
        enqueue(self.factory, "run", {"item_id": selected_item_id} if selected_item_id else {},
                f"rollover:{batch_id}:{today}")
        with transaction(self.factory) as session:
            for item in session.scalars(select(Item).where(Item.batch_id == batch_id, Item.phase == "proposed")):
                item.status, item.phase = "待处理", "blocked"
                item.reason, item.updated_at = "已跨北京时间业务日，已排队重新取数计算", stamp()

    def _hold(self, item_id, reason, status="待处理"):
        with transaction(self.factory) as session:
            item = session.get(Item, item_id)
            item.status, item.phase, item.reason, item.updated_at = status, "blocked", reason, stamp()

    def _claim(self, item_id, decision):
        identity = decision.key.target_identity()
        # 先确保锁行存在；并发创建的冲突只需重新读取，不影响已有执行。
        try:
            with transaction(self.factory) as session:
                if session.get(VariantGuard, identity) is None:
                    session.add(VariantGuard(variant_id=identity))
        except IntegrityError:
            pass
        execution_id = str(uuid4())
        with transaction(self.factory) as session:
            if session.scalar(select(Execution.id).where(Execution.item_id == item_id)):
                return None
            if self.config.coordination_required:
                control = lock_control(session, decision.key)
                extra=[]
                if decision.trace.get('correction_id'):
                    from app.models.coordination import Correction
                    correction=session.get(Correction,decision.trace['correction_id'])
                    if correction:extra=[correction.requested_by,correction.approved_by]
                locked=lock_authorization(session,decision.key,extra)
                if decision.trace.get("correction_id"):
                    from app.services.corrections import correction_gate
                    allowed, reason = correction_gate(session, decision, control,locked=locked)
                else:
                    allowed, reason = automation_gate(session, decision.key, decision.trace.get("authorization", {}),locked=locked)
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
                item.status, item.phase = "本轮不调", "excluded"
                item.reason = "提交前命中人工保护名单：" + protection.reason
                item.updated_at = stamp()
                return None
            cutoff = stamp(utcnow() - timedelta(hours=72))
            conditions = [VariantGuard.variant_id == identity, VariantGuard.execution_id.is_(None)]
            if not decision.trace.get("correction_id"):
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
        return execution_id

    def _submit(self, item_id, decision, business_date=None):
        if business_date and utcnow().astimezone(BEIJING).date() != business_date:
            return False
        allowed, reason = self._allowed(decision.key, manual=bool(decision.trace.get("correction_id")))
        if not allowed:
            self._hold(item_id, reason)
            return
        execution_id = self._claim(item_id, decision)
        if not execution_id:
            return
        # 持久化可能恰好跨日；在外部调用前再次核验。此时尚未调用外部，可明确终结本地意图。
        if business_date and utcnow().astimezone(BEIJING).date() != business_date:
            with transaction(self.factory) as session:
                execution = session.get(Execution, execution_id)
                execution.state, execution.updated_at = "failed", stamp()
                execution.response = {"status": "not_submitted", "message": "跨日，未调用上传接口"}
                guard = session.get(VariantGuard, execution.variant_id)
                if guard.execution_id == execution_id:
                    guard.execution_id = None
            self._hold(item_id, "已跨北京时间业务日，等待重新取数计算")
            return False
        # 执行意图已在上个事务持久化。最后一段与人工接管串行化：
        # 请求已开始后接管不会撤销外部调用；崩溃时上个意图仍在，继续核验不重传。
        with transaction(self.factory) as session:
            if self.config.coordination_required:
                control = lock_control(session, decision.key)
                extra=[]
                if decision.trace.get('correction_id'):
                    from app.models.coordination import Correction
                    correction=session.get(Correction,decision.trace['correction_id'])
                    if correction:extra=[correction.requested_by,correction.approved_by]
                locked=lock_authorization(session,decision.key,extra)
                if decision.trace.get("correction_id"):
                    from app.services.corrections import correction_gate
                    allowed, reason = correction_gate(session, decision, control,locked=locked)
                else:
                    allowed, reason = automation_gate(session, decision.key, decision.trace.get("authorization", {}),locked=locked)
                if not allowed:
                    execution, item = session.get(Execution, execution_id), session.get(Item, item_id)
                    execution.state, execution.updated_at = "failed", stamp()
                    execution.response = {"status": "not_submitted", "message": reason}
                    session.get(VariantGuard, execution.variant_id).execution_id = None
                    item.status, item.phase, item.reason, item.updated_at = "待处理", "blocked", reason, stamp()
                    return
            try:
                result = self.adapter.submit(execution_id, decision)
            except Exception as exc:
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

    def reconcile(self, item_id=None, target_id=None):
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
        batches = set()
        for execution in pending:
            decision = Decision.model_validate(execution.request)
            try:
                result = self.adapter.lookup(execution.id, execution.task_id, decision.key)
            except Exception:
                continue  # 稍后继续核验，保持原锁，不把查询失败当作未提交。
            with transaction(self.factory) as session:
                current = session.get(Execution, execution.id)
                if current.state not in {"submitting", "accepted", "unknown", "mismatch"}:
                    continue
                item = session.get(Item, current.item_id)
                if self.config.coordination_required:
                    lock_control(session, decision.key)
                # 迁移前同一目标可能已有多个未知执行；锁行串行化全部历史执行的释放。
                guard = session.scalar(select(VariantGuard).where(
                    VariantGuard.variant_id == current.variant_id).with_for_update())
                batches.add(item.batch_id)
                if result.status == "rejected":
                    current.state, item.phase, item.status = "failed", "failed", "待处理"
                    current.evidence = {"row_status": "rejected", "message": result.message}
                    item.reason = result.message or "逐行处理已确认失败，需重新评估"
                elif (result.status == "accepted" and result.actual_price is not None
                      and result.actual_price.is_finite() and result.actual_price > 0
                      and result.price_basis == "actual_including_vat" and result.observed_at is not None
                      and result.observed_at.tzinfo is not None
                      and parse_stamp(current.created_at) <= result.observed_at <= utcnow() + timedelta(seconds=30)):
                    # 新判定保留精确目标比值，核验不能用50位展示值或默认28位上下文跨越容限。
                    # 旧执行没有精确字段时仅按原持久目标核验，不回算或改写历史拟价。
                    exact = decision.trace.get("exact_target_actual")
                    target = (MoneyRatio(Decimal(exact["numerator"]), Decimal(exact["denominator"]))
                              if exact else MoneyRatio(decision.target_actual))
                    difference_ratio = (MoneyRatio(result.actual_price) - target).absolute()
                    difference = difference_ratio.decimal()
                    current.evidence = {"actual_price": str(result.actual_price), "target_actual": str(decision.target_actual),
                        "difference": str(difference), "tolerance": str(allowed_error(decision.key.site)),
                        "observed_at": stamp(result.observed_at), "price_basis": result.price_basis,
                        "message": result.message, "mode": self.adapter.mode}
                    if difference_ratio <= MoneyRatio(allowed_error(decision.key.site)):
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
                        item.reason = "模拟价格已核验" if self.adapter.mode == "demo" else "当前实际有效售价已逐条核验"
                        # 较旧请求晚核验不得倒退最近生效时间。
                        guard.last_effective_at = max(filter(None, [guard.last_effective_at, stamp(effective)]))
                        if self.config.coordination_required:
                            control = lock_control(session, decision.key)
                            verified_baseline(control, decision, result.actual_price)
                    else:
                        current.state, item.phase, item.status = "mismatch", "mismatch", "待核验"
                        item.reason = "实际售价与目标超出误差范围，已暂停本站点新提交"
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
                config = settings_value(self.factory)
                paused_sites = sorted(set(config["paused_sites"]) | {decision.key.site})
                update_settings(self.factory, {"paused_sites": paused_sites}, "worker")
        for batch_id in batches:
            self._summarize(batch_id)

    def _summarize(self, batch_id):
        with transaction(self.factory) as session:
            batch = session.get(Batch, batch_id)
            counts = Counter(session.scalars(select(Item.status).where(Item.batch_id == batch_id)))
            batch.summary = dict(counts)
            batch.status = "pending_verification" if counts["待核验"] else "completed"
        if self.config.coordination_required:
            from app.services.notifications import enqueue_batch_notices
            enqueue_batch_notices(self.factory, batch_id)
