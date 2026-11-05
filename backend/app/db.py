from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database(url: str):
    engine = create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_setup(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def transaction(factory):
    with factory() as session:
        with session.begin():
            yield session
