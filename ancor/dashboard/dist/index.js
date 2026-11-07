(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!SDK || !registry || typeof registry.register !== "function") return;
  const React = SDK.React;
  const h = React.createElement;
  const hooks = SDK.hooks || {};
  const useState = hooks.useState || React.useState;
  const useEffect = hooks.useEffect || React.useEffect;
  const Button = (SDK.components && SDK.components.Button) || "button";
  const esc = (value) => String(value == null ? "" : value);
  function fetchData(path) {
    const url = "/api/plugins/ancor/" + path;
    if (typeof SDK.fetchJSON === "function") return SDK.fetchJSON(url);
    if (typeof SDK.authedFetch === "function") {
      return SDK.authedFetch(url).then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
    }
    return Promise.reject(new Error("当前 Hermes 未提供鉴权请求接口，请更新 Hermes。"));
  }
  function List(props) {
    const items = props.items || [];
    return h("div", { className: "ancor-list" }, items.length ? items.map((item) => h("article", { className: "ancor-item", key: item.id },
      h("button", { onClick: () => props.onOpen(item.detail_kind || props.kind, item.id) }, esc(item.title || item.kind || item.id)),
      h("span", { className: "ancor-badge" }, esc(item.status || item.state || "")),
      item.generation != null ? h("small", null, "请求版本 " + item.generation + " · 已评估 " + item.observed_generation) : null)) : h("p", { className: "ancor-muted" }, "暂无数据"));
  }
  function Detail(props) {
    const detail = props.value;
    if (!detail) return null;
    const req = detail.request;
    if (!req) return h("section", null, h(Button, { onClick: props.onClose }, "关闭详情"), h("pre", { style: { whiteSpace: "pre-wrap" } }, JSON.stringify(detail, null, 2)));
    const labels = { outcome: "目标", constraint: "约束", advice: "建议", context: "背景" };
    return h("section", { className: "ancor-card" },
      h(Button, { onClick: props.onClose }, "关闭详情"), h("h2", null, req.title),
      h("p", null, "状态：" + req.status + " · 请求版本：" + req.generation + " · 已评估：" + req.observed_generation),
      req.content_mode === "legacy" ? h("pre", { style: { whiteSpace: "pre-wrap" } }, req.brief) :
        Object.entries(labels).map(([key, label]) => h("section", { key }, h("h3", null, label),
          h("ul", null, (detail.fields[key] || []).map((point) => h("li", { key: point.version_id }, point.body))))),
      h("h3", null, "问题"), h("ul", null, (detail.questions || []).map((q) => h("li", { key: q.id }, q.body + " · " + q.state))),
      h("h3", null, "任务调整"), h("ul", null, (detail.coordination || []).map((r) => h("li", { key: r.task_id }, r.decision + " · " + r.state + " · " + r.reason))),
      h("h3", null, "改动历史"), h("ul", null, ((detail.changes || {}).items || []).map((c) => h("li", { key: c.id },
        h("span", null, c.actor + " · " + new Date(c.committed_at * 1000).toLocaleString()),
        h("pre", { style: { whiteSpace: "pre-wrap" } }, JSON.stringify(c.operations, null, 2))))),
      detail.changes && detail.changes.truncated ? h("p", null, "仅展示最近 50 次改动。完整历史可通过请求历史工具分页查看。") : null);
  }
  function AncorPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState("");
    const [selected, setSelected] = useState(null);
    const [detail, setDetail] = useState(null);
    const load = () => fetchData("overview").then((value) => { setData(value); setError(""); }).catch((e) => setError(e.message));
    useEffect(() => { load(); const timer = setInterval(load, 5000); return () => clearInterval(timer); }, []);
    useEffect(() => {
      if (!selected) { setDetail(null); return; }
      let active = true;
      setDetail(null);
      const refresh = () => fetchData(selected.kind + "/" + encodeURIComponent(selected.id)).then((value) => {
        if (active) setDetail(value);
      }).catch((e) => { if (active) setError(e.message); });
      refresh();
      const timer = setInterval(refresh, 5000);
      return () => { active = false; clearInterval(timer); };
    }, [selected]);
    const sections = data ? [["请求", data.requests, "requests"], ["任务", data.tasks, "tasks"], ["待回答问题", data.questions, "notifications"], ["事件", data.events, "events"]] : [];
    return h("section", { className: "ancor-app" },
      h("div", { className: "ancor-header" }, h("div", null, h("h1", null, "ANCOR"), h("p", { className: "ancor-muted" }, "只读请求与协调状态")), h(Button, { className: "ancor-refresh", onClick: load }, "刷新")),
      h("div", { className: "ancor-cards" }, sections.map(([label, value]) => h("div", { className: "ancor-card", key: label }, h("span", null, label), h("b", null, value.count)))),
      h(Detail, { value: detail, onClose: () => setSelected(null) }),
      h("div", { className: "ancor-grid" }, sections.map(([label, value, kind]) => h("section", { key: label }, h("h2", null, label), h(List, { items: value.items, kind, onOpen: (kind, id) => setSelected({ kind, id }) })))),
      h("p", { className: "ancor-status", "aria-live": "polite" }, error ? "加载失败：" + error : "已更新 · 每 5 秒轮询 · 只读"));
  }
  registry.register("ancor", AncorPage);
})();
