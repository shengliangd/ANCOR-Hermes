"""Exercise page effects and user navigation through the host's authenticated SDK."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def page():
    quickjs = pytest.importorskip('quickjs')
    ctx = quickjs.Context()
    ctx.eval('''
        var component, tree, states = [], stateIndex = 0;
        var dependencies = [], effects = [], effectIndex = 0;
        var calls = [], failures = {}, rawFetchCalls = 0;
        var overview = {
          requests: {count: 1, items: [{id: "request /中文", title: "Report", status: "active"}]},
          tasks: {count: 0, items: []}, questions: {count: 0, items: []},
          events: {count: 0, items: []}
        };
        var detail = {request: {title: "Report detail", status: "active", generation: 1,
          observed_generation: 1}, fields: {outcome: [{version_id: "v1", body: "Deliver report"}]}};
        function response(url) { return url.endsWith("/overview") ? overview : detail; }
        function fetch() { rawFetchCalls++; throw new Error("Unauthenticated fetch used"); }
        function setInterval() { return 1; }
        function clearInterval() {}
        var SDK = {
          fetchJSON: function(url) {
            calls.push({helper: "fetchJSON", url: url});
            return failures[url] ? Promise.reject(new Error(failures[url])) : Promise.resolve(response(url));
          },
          authedFetch: function(url) {
            calls.push({helper: "authedFetch", url: url});
            return Promise.resolve({ok: !failures[url], status: failures[url] || 200,
              json: function() { return Promise.resolve(response(url)); }});
          },
          React: {
            createElement: function(type, props) {
              return {type: type, props: props || {}, children: Array.prototype.slice.call(arguments, 2)};
            },
            useState: function(initial) {
              var i = stateIndex++;
              if (!(i in states)) states[i] = initial;
              return [states[i], function(value) { states[i] = value; }];
            },
            useEffect: function(callback, deps) {
              var i = effectIndex++, key = JSON.stringify(deps);
              if (dependencies[i] !== key) { dependencies[i] = key; effects.push(callback); }
            }
          }
        };
        var window = {__HERMES_PLUGIN_SDK__: SDK,
          __HERMES_PLUGINS__: {register: function(name, page) { component = page; }}};
        function materialize(node) {
          if (Array.isArray(node)) return node.map(materialize);
          if (!node || typeof node !== "object") return node;
          if (typeof node.type === "function") return materialize(node.type(node.props));
          return {type: node.type, props: node.props, children: node.children.map(materialize)};
        }
        function render() {
          stateIndex = 0; effectIndex = 0; effects = [];
          tree = materialize(component());
          effects.forEach(function(callback) { callback(); });
        }
        function findButton(node, label) {
          if (Array.isArray(node)) {
            for (var child of node) { var found = findButton(child, label); if (found) return found; }
          } else if (node && typeof node === "object") {
            if (node.type === "button" && node.children.indexOf(label) !== -1) return node;
            return findButton(node.children, label);
          }
          return null;
        }
        function click(label) {
          var button = findButton(tree, label);
          if (!button) throw new Error("Missing button " + label);
          button.props.onClick();
        }
    ''')
    ctx.eval((Path(__file__).parents[1] / 'ancor/dashboard/dist/index.js').read_text())
    return ctx


def settle(page):
    for _ in range(100):
        if not page.execute_pending_job():
            return
    pytest.fail('Page promise queue did not settle')


@pytest.mark.parametrize('helper', ['fetchJSON', 'authedFetch'])
def test_overview_and_detail_use_authenticated_host_requests(page, helper):
    if helper == 'authedFetch':
        page.eval('delete SDK.fetchJSON;')
    page.eval('render();')
    settle(page)
    page.eval('render(); click("Report"); render();')
    settle(page)
    page.eval('render();')
    calls = json.loads(page.eval('JSON.stringify(calls)'))
    assert calls == [
        {'helper': helper, 'url': '/api/plugins/ancor/overview'},
        {'helper': helper, 'url': '/api/plugins/ancor/requests/request%20%2F%E4%B8%AD%E6%96%87'},
    ]
    assert 'Deliver report' in page.eval('JSON.stringify(tree)')
    assert page.eval('rawFetchCalls') == 0


@pytest.mark.parametrize('helper', ['fetchJSON', 'authedFetch'])
@pytest.mark.parametrize('endpoint', ['overview', 'detail'])
def test_request_failures_are_visible_without_unauthenticated_retry(page, helper, endpoint):
    if helper == 'authedFetch':
        page.eval('delete SDK.fetchJSON;')
    path = ('/api/plugins/ancor/overview' if endpoint == 'overview' else
            '/api/plugins/ancor/requests/request%20%2F%E4%B8%AD%E6%96%87')
    page.eval(f'failures[{json.dumps(path)}] = 401; render();')
    settle(page)
    if endpoint == 'detail':
        page.eval('render(); click("Report"); render();')
        settle(page)
    page.eval('render();')
    rendered = page.eval('JSON.stringify(tree)')
    assert '加载失败：' in rendered and '401' in rendered
    assert page.eval('rawFetchCalls') == 0


def test_missing_authenticated_sdk_reports_compatibility_error(page):
    page.eval('delete SDK.fetchJSON; delete SDK.authedFetch; render();')
    settle(page)
    page.eval('render();')
    assert '当前 Hermes 未提供鉴权请求接口，请更新 Hermes。' in page.eval('JSON.stringify(tree)')
    assert json.loads(page.eval('JSON.stringify(calls)')) == []
    assert page.eval('rawFetchCalls') == 0
