"""Gate 1 DB tests: tenant isolation and rollback fixture behaviour.

WHAT: Proves tenant-filtered queries never cross tenants and that fixtures roll back.
WHERE: Exercises `tests/conftest.py` and the pattern Spec 002 will formalise.
WHY: Tenant isolation is the top guardrail; testing it from day one sets the pattern.
"""


def test_tenant_isolation(db, tenant_table):
    with db.cursor() as cur:
        cur.execute("INSERT INTO notes (tenant_id, body) VALUES ('A','a1'),('B','b1')")
        cur.execute("SELECT body FROM notes WHERE tenant_id = %s", ("B",))
        assert [r[0] for r in cur.fetchall()] == ["b1"]


def test_rollback_a_writes(db):
    with db.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS _rb_probe (v int)")
        cur.execute("INSERT INTO _rb_probe VALUES (1)")


def test_rollback_b_sees_nothing(db):
    # Runs after the test above (file order): its table and row must be gone.
    with db.cursor() as cur:
        cur.execute("SELECT to_regclass('_rb_probe')")
        assert cur.fetchone()[0] is None
