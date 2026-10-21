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
  const Card = SDK.components && SDK.components.Card;
  const Button = (SDK.components && SDK.components.Button) || "button";
  const esc = (value) => String(value == null ? "" : value);
  function fetchOverview() {
    return fetch("/api/plugins/ancor/overview", { credentials: "same-origin" }).then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function List(props) {
    const items = props.items || [];
    return h("div", { className: "ancor-list" }, items.length ? items.map((item) => h("article", { className: "ancor-item", key: item.id },
      h("strong", null, esc(item.title || item.kind || item.id)),
      h("span", { className: "ancor-badge" }, esc(item.status || item.state || "")),
      h("small", null, esc(item.id)))) : h("p", { className: "ancor-muted" }, "暂无数据"));
  }
  function AncorPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState("");
    const load = () => fetchOverview().then((value) => { setData(value); setError(""); }).catch((e) => setError(e.message));
    useEffect(() => { load(); const timer = setInterval(load, 5000); return () => clearInterval(timer); }, []);
    const sections = data ? [["需求", data.requirements], ["任务", data.tasks], ["待回答问题", data.questions], ["事件", data.events]] : [];
    return h("section", { className: "ancor-app" },
      h("div", { className: "ancor-header" }, h("div", null, h("h1", null, "ANCOR"), h("p", { className: "ancor-muted" }, "只读需求与协调状态")), h(Button, { className: "ancor-refresh", onClick: load }, "刷新")),
      h("div", { className: "ancor-cards" }, sections.map(([label, value]) => h("div", { className: "ancor-card", key: label }, h("span", null, label), h("b", null, value.count)))),
      h("div", { className: "ancor-grid" }, sections.map(([label, value]) => h("section", { key: label }, h("h2", null, label), h(List, { items: value.items })))),
      h("p", { className: "ancor-status", "aria-live": "polite" }, error ? "加载失败：" + error : "已更新 · 每 5 秒轮询 · 只读"));
  }
  registry.register("ancor", AncorPage);
})();
