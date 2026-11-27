"""独立查询已上传任务的结果，不查询商品现价，不执行上传。"""
import time
from app.observability import bind_sink, event


def main():
    from app.main import create_app
    app = create_app(log_role='worker')
    try:
        with bind_sink(app.state.log_sink):
            while True:
                try:
                    app.state.service.reconcile(tasks_only=True)
                except Exception as exc:
                    event('task_results.failed', exception=exc)
                time.sleep(60)
    finally:
        app.state.service.close()
        app.state.engine.dispose()


if __name__ == '__main__':
    main()
