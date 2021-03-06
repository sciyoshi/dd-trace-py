import MySQLdb

from ddtrace import Pin
from ddtrace.contrib.mysqldb.patch import patch, unpatch

from nose.tools import eq_

from ..config import MYSQL_CONFIG
from ...util import assert_dict_issuperset
from ...test_tracer import get_dummy_tracer


class MySQLCore(object):
    """Base test case for MySQL drivers"""
    conn = None
    TEST_SERVICE = 'test-mysql'

    def setUp(self):
        patch()

    def tearDown(self):
        # Reuse the connection across tests
        if self.conn:
            try:
                self.conn.ping()
            except MySQLdb.InterfaceError:
                pass
            else:
                self.conn.close()
        unpatch()

    def _get_conn_tracer(self):
        # implement me
        pass

    def test_simple_query(self):
        conn, tracer = self._get_conn_tracer()
        writer = tracer.writer
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        rows = cursor.fetchall()
        eq_(len(rows), 1)
        spans = writer.pop()
        eq_(len(spans), 1)

        span = spans[0]
        eq_(span.service, self.TEST_SERVICE)
        eq_(span.name, 'mysql.query')
        eq_(span.span_type, 'sql')
        eq_(span.error, 0)
        assert_dict_issuperset(span.meta, {
            'out.host': u'127.0.0.1',
            'out.port': u'3306',
            'db.name': u'test',
            'db.user': u'test',
            'sql.query': u'SELECT 1',
        })

    def test_simple_query_with_positional_args(self):
        conn, tracer = self._get_conn_tracer_with_positional_args()
        writer = tracer.writer
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        rows = cursor.fetchall()
        eq_(len(rows), 1)
        spans = writer.pop()
        eq_(len(spans), 1)

        span = spans[0]
        eq_(span.service, self.TEST_SERVICE)
        eq_(span.name, 'mysql.query')
        eq_(span.span_type, 'sql')
        eq_(span.error, 0)
        assert_dict_issuperset(span.meta, {
            'out.host': u'127.0.0.1',
            'out.port': u'3306',
            'db.name': u'test',
            'db.user': u'test',
            'sql.query': u'SELECT 1',
        })

    def test_query_with_several_rows(self):
        conn, tracer = self._get_conn_tracer()
        writer = tracer.writer
        cursor = conn.cursor()
        query = "SELECT n FROM (SELECT 42 n UNION SELECT 421 UNION SELECT 4210) m"
        cursor.execute(query)
        rows = cursor.fetchall()
        eq_(len(rows), 3)
        spans = writer.pop()
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.get_tag('sql.query'), query)

    def test_query_many(self):
        # tests that the executemany method is correctly wrapped.
        conn, tracer = self._get_conn_tracer()
        writer = tracer.writer
        tracer.enabled = False
        cursor = conn.cursor()

        cursor.execute("""
            create table if not exists dummy (
                dummy_key VARCHAR(32) PRIMARY KEY,
                dummy_value TEXT NOT NULL)""")
        tracer.enabled = True

        stmt = "INSERT INTO dummy (dummy_key, dummy_value) VALUES (%s, %s)"
        data = [
            ("foo","this is foo"),
            ("bar","this is bar"),
        ]
        cursor.executemany(stmt, data)
        query = "SELECT dummy_key, dummy_value FROM dummy ORDER BY dummy_key"
        cursor.execute(query)
        rows = cursor.fetchall()
        eq_(len(rows), 2)
        eq_(rows[0][0], "bar")
        eq_(rows[0][1], "this is bar")
        eq_(rows[1][0], "foo")
        eq_(rows[1][1], "this is foo")

        spans = writer.pop()
        eq_(len(spans), 2)
        span = spans[-1]
        eq_(span.get_tag('sql.query'), query)
        cursor.execute("drop table if exists dummy")

    def test_query_proc(self):
        conn, tracer = self._get_conn_tracer()
        writer = tracer.writer

        # create a procedure
        tracer.enabled = False
        cursor = conn.cursor()
        cursor.execute("DROP PROCEDURE IF EXISTS sp_sum")
        cursor.execute("""
            CREATE PROCEDURE sp_sum (IN p1 INTEGER, IN p2 INTEGER, OUT p3 INTEGER)
            BEGIN
                SET p3 := p1 + p2;
            END;""")

        tracer.enabled = True
        proc = "sp_sum"
        data = (40, 2, None)
        output = cursor.callproc(proc, data)
        eq_(len(output), 3)
        # resulted p3 isn't stored on output[2], we need to fetch it with select
        # http://mysqlclient.readthedocs.io/user_guide.html#cursor-objects
        cursor.execute("SELECT @_sp_sum_2;")
        eq_(cursor.fetchone()[0], 42)

        spans = writer.pop()
        assert spans, spans

        # number of spans depends on MySQL implementation details,
        # typically, internal calls to execute, but at least we
        # can expect the next to the last closed span to be our proc.
        span = spans[-2]
        eq_(span.service, self.TEST_SERVICE)
        eq_(span.name, 'mysql.query')
        eq_(span.span_type, 'sql')
        eq_(span.error, 0)
        assert_dict_issuperset(span.meta, {
            'out.host': u'127.0.0.1',
            'out.port': u'3306',
            'db.name': u'test',
            'db.user': u'test',
            'sql.query': u'sp_sum',
        })


class TestMysqlPatch(MySQLCore):
    """Ensures MysqlDB is properly patched"""

    def _connect_with_kwargs(self):
        return MySQLdb.Connect(**{
            'host': MYSQL_CONFIG['host'],
            'user': MYSQL_CONFIG['user'],
            'passwd': MYSQL_CONFIG['password'],
            'db': MYSQL_CONFIG['database'],
            'port': MYSQL_CONFIG['port'],
        })

    def _get_conn_tracer(self):
        if not self.conn:
            tracer = get_dummy_tracer()
            self.conn = self._connect_with_kwargs()
            self.conn.ping()
            # Ensure that the default pin is there, with its default value
            pin = Pin.get_from(self.conn)
            assert pin
            assert pin.service == 'mysql'
            # Customize the service
            # we have to apply it on the existing one since new one won't inherit `app`
            pin.clone(service=self.TEST_SERVICE, tracer=tracer).onto(self.conn)

            return self.conn, tracer

    def _get_conn_tracer_with_positional_args(self):
        if not self.conn:
            tracer = get_dummy_tracer()
            self.conn = MySQLdb.Connect(
                MYSQL_CONFIG['host'],
                MYSQL_CONFIG['user'],
                MYSQL_CONFIG['password'],
                MYSQL_CONFIG['database'],
                MYSQL_CONFIG['port'],
            )
            self.conn.ping()
            # Ensure that the default pin is there, with its default value
            pin = Pin.get_from(self.conn)
            assert pin
            assert pin.service == 'mysql'
            # Customize the service
            # we have to apply it on the existing one since new one won't inherit `app`
            pin.clone(service=self.TEST_SERVICE, tracer=tracer).onto(self.conn)

            return self.conn, tracer

    def test_patch_unpatch(self):
        unpatch()
        # assert we start unpatched
        conn = self._connect_with_kwargs()
        assert not Pin.get_from(conn)
        conn.close()

        patch()
        try:
            tracer = get_dummy_tracer()
            writer = tracer.writer
            conn = self._connect_with_kwargs()
            pin = Pin.get_from(conn)
            assert pin
            pin.clone(service=self.TEST_SERVICE, tracer=tracer).onto(conn)
            conn.ping()

            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            rows = cursor.fetchall()
            eq_(len(rows), 1)
            spans = writer.pop()
            eq_(len(spans), 1)

            span = spans[0]
            eq_(span.service, self.TEST_SERVICE)
            eq_(span.name, 'mysql.query')
            eq_(span.span_type, 'sql')
            eq_(span.error, 0)
            assert_dict_issuperset(span.meta, {
                'out.host': u'127.0.0.1',
                'out.port': u'3306',
                'db.name': u'test',
                'db.user': u'test',
                'sql.query': u'SELECT 1',
            })

        finally:
            unpatch()

            # assert we finish unpatched
            conn = self._connect_with_kwargs()
            assert not Pin.get_from(conn)
            conn.close()

        patch()
