# -*- coding: utf-8 -*-
# stdlib
import time
import logging
import os
import re

# 3p
from flask import Flask, render_template
from nose.tools import eq_


# project
from ddtrace import Tracer
from ddtrace.constants import SAMPLING_PRIORITY_KEY
from ddtrace.contrib.flask import TraceMiddleware
from ddtrace.ext import http, errors
from ...test_tracer import DummyWriter


log = logging.getLogger(__name__)

# global writer tracer for the tests.
writer = DummyWriter()
tracer = Tracer()
tracer.writer = writer


class TestError(Exception): pass
class HandleMe(Exception): pass


# define a toy flask app.
cur_dir = os.path.dirname(os.path.realpath(__file__))
tmpl_path = os.path.join(cur_dir, 'test_templates')

app = Flask(__name__, template_folder=tmpl_path)


@app.route('/')
def index():
    return 'hello'


@app.route('/error')
def error():
    raise TestError()

@app.route('/handleme')
def handle_me():
    raise HandleMe()

@app.route('/fatal')
def fatal():
    1 / 0


@app.route('/tmpl')
def tmpl():
    return render_template('test.html', world="earth")


@app.route('/tmpl/err')
def tmpl_err():
    return render_template('err.html')

@app.route('/tmpl/render_err')
def tmpl_render_err():
    return render_template('render_err.html')

@app.route('/child')
def child():
    with tracer.trace('child') as span:
        span.set_tag('a', 'b')
        return 'child'

@app.route("/custom_span")
def custom_span():
    span = tracer.current_span()
    assert span
    span.resource = "overridden"
    return 'hiya'


def unicode_view():
    return u'üŋïĉóđē'

# DEV: Manually register endpoint so we can control the endpoint name
app.add_url_rule(
    u'/üŋïĉóđē',
    u'üŋïĉóđē',
    unicode_view,
)


@app.errorhandler(TestError)
def handle_my_exception(e):
    assert isinstance(e, TestError)
    return 'error', 500

@app.errorhandler(HandleMe)
def err_to_202(e):
    assert isinstance(e, HandleMe)
    return 'handled', 202


# add tracing to the app (we use a global app to help ensure multiple requests
# work)
service = "test.flask.service"
assert not writer.pop()  # should always be empty
traced_app = TraceMiddleware(app, tracer, service=service, distributed_tracing=True)

# make the app testable
app.config['TESTING'] = True
app = app.test_client()


class TestFlask(object):

    def setUp(self):
        # ensure the last test didn't leave any trash
        writer.pop()

    def test_child(self):
        start = time.time()
        rv = app.get('/child')
        end = time.time()
        # ensure request worked
        eq_(rv.status_code, 200)
        eq_(rv.data, b'child')
        # ensure trace worked
        spans = writer.pop()
        eq_(len(spans), 2)

        spans_by_name = {s.name:s for s in spans}

        s = spans_by_name['flask.request']
        assert s.span_id
        assert s.trace_id
        assert not s.parent_id
        eq_(s.service, service)
        eq_(s.resource, "child")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 0)

        c = spans_by_name['child']
        assert c.span_id
        eq_(c.trace_id, s.trace_id)
        eq_(c.parent_id, s.span_id)
        eq_(c.service, service)
        eq_(c.resource, 'child')
        assert c.start >= start
        assert c.duration <= end - start
        eq_(c.error, 0)

    def test_success(self):
        start = time.time()
        rv = app.get('/')
        end = time.time()

        # ensure request worked
        eq_(rv.status_code, 200)
        eq_(rv.data, b'hello')

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]
        eq_(s.service, service)
        eq_(s.resource, "index")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 0)
        eq_(s.meta.get(http.STATUS_CODE), '200')
        eq_(s.meta.get(http.METHOD), 'GET')

        services = writer.pop_services()
        expected = {
            service : {"app":"flask", "app_type":"web"}
        }
        eq_(services, expected)

    def test_template(self):
        start = time.time()
        rv = app.get('/tmpl')
        end = time.time()

        # ensure request worked
        eq_(rv.status_code, 200)
        eq_(rv.data, b'hello earth')

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 2)
        by_name = {s.name:s for s in spans}
        s = by_name["flask.request"]
        eq_(s.service, service)
        eq_(s.resource, "tmpl")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 0)
        eq_(s.meta.get(http.STATUS_CODE), '200')
        eq_(s.meta.get(http.METHOD), 'GET')

        t = by_name["flask.template"]
        eq_(t.get_tag("flask.template"), "test.html")
        eq_(t.parent_id, s.span_id)
        eq_(t.trace_id, s.trace_id)
        assert s.start < t.start < t.start + t.duration < end

    def test_handleme(self):
        start = time.time()
        rv = app.get('/handleme')
        end = time.time()

        # ensure request worked
        eq_(rv.status_code, 202)
        eq_(rv.data, b'handled')

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]
        eq_(s.service, service)
        eq_(s.resource, "handle_me")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 0)
        eq_(s.meta.get(http.STATUS_CODE), '202')
        eq_(s.meta.get(http.METHOD), 'GET')

    def test_template_err(self):
        start = time.time()
        try:
            app.get('/tmpl/err')
        except Exception:
            pass
        else:
            assert 0
        end = time.time()

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 1)
        by_name = {s.name:s for s in spans}
        s = by_name["flask.request"]
        eq_(s.service, service)
        eq_(s.resource, "tmpl_err")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 1)
        eq_(s.meta.get(http.STATUS_CODE), '500')
        eq_(s.meta.get(http.METHOD), 'GET')

    def test_template_render_err(self):
        tracer.debug_logging = True
        start = time.time()
        try:
            app.get('/tmpl/render_err')
        except Exception:
            pass
        else:
            assert 0
        end = time.time()

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 2)
        by_name = {s.name:s for s in spans}
        s = by_name["flask.request"]
        eq_(s.service, service)
        eq_(s.resource, "tmpl_render_err")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 1)
        eq_(s.meta.get(http.STATUS_CODE), '500')
        eq_(s.meta.get(http.METHOD), 'GET')
        t = by_name["flask.template"]
        eq_(t.get_tag("flask.template"), "render_err.html")
        eq_(t.error, 1)
        eq_(t.parent_id, s.span_id)
        eq_(t.trace_id, s.trace_id)

    def test_error(self):
        start = time.time()
        rv = app.get('/error')
        end = time.time()

        # ensure the request itself worked
        eq_(rv.status_code, 500)
        eq_(rv.data, b'error')

        # ensure the request was traced.
        assert not tracer.current_span()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]
        eq_(s.service, service)
        eq_(s.resource, "error")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.meta.get(http.STATUS_CODE), '500')
        eq_(s.meta.get(http.METHOD), 'GET')

    def test_fatal(self):
        if not traced_app.use_signals:
            return

        start = time.time()
        try:
            app.get('/fatal')
        except ZeroDivisionError:
            pass
        else:
            assert 0
        end = time.time()

        # ensure the request was traced.
        assert not tracer.current_span()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]
        eq_(s.service, service)
        eq_(s.resource, "fatal")
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.meta.get(http.STATUS_CODE), '500')
        eq_(s.meta.get(http.METHOD), 'GET')
        assert "ZeroDivisionError" in s.meta.get(errors.ERROR_TYPE), s.meta
        assert "by zero" in s.meta.get(errors.ERROR_MSG)
        assert re.search('File ".*/contrib/flask/test_flask.py", line [0-9]+, in fatal', s.meta.get(errors.ERROR_STACK))

    def test_unicode(self):
        start = time.time()
        rv = app.get(u'/üŋïĉóđē')
        end = time.time()

        # ensure request worked
        eq_(rv.status_code, 200)
        eq_(rv.data, b'\xc3\xbc\xc5\x8b\xc3\xaf\xc4\x89\xc3\xb3\xc4\x91\xc4\x93')

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]
        eq_(s.service, service)
        eq_(s.resource, u'üŋïĉóđē')
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 0)
        eq_(s.meta.get(http.STATUS_CODE), '200')
        eq_(s.meta.get(http.METHOD), 'GET')
        eq_(s.meta.get(http.URL), u'http://localhost/üŋïĉóđē')

    def test_404(self):
        start = time.time()
        rv = app.get(u'/404/üŋïĉóđē')
        end = time.time()

        # ensure that we hit a 404
        eq_(rv.status_code, 404)

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]
        eq_(s.service, service)
        eq_(s.resource, u'404')
        assert s.start >= start
        assert s.duration <= end - start
        eq_(s.error, 0)
        eq_(s.meta.get(http.STATUS_CODE), '404')
        eq_(s.meta.get(http.METHOD), 'GET')
        eq_(s.meta.get(http.URL), u'http://localhost/404/üŋïĉóđē')

    def test_propagation(self):
        rv = app.get('/', headers={
            'x-datadog-trace-id': '1234',
            'x-datadog-parent-id': '4567',
            'x-datadog-sampling-priority': '2'
        })

        # ensure request worked
        eq_(rv.status_code, 200)
        eq_(rv.data, b'hello')

        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]

        # ensure the propagation worked well
        eq_(s.trace_id, 1234)
        eq_(s.parent_id, 4567)
        eq_(s.get_metric(SAMPLING_PRIORITY_KEY), 2)

    def test_custom_span(self):
        rv = app.get('/custom_span')
        eq_(rv.status_code, 200)
        # ensure trace worked
        assert not tracer.current_span(), tracer.current_span().pprint()
        spans = writer.pop()
        eq_(len(spans), 1)
        s = spans[0]
        eq_(s.service, service)
        eq_(s.resource, "overridden")
        eq_(s.error, 0)
        eq_(s.meta.get(http.STATUS_CODE), '200')
        eq_(s.meta.get(http.METHOD), 'GET')



