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

  function BudgetChips(props) {
    const budget = props.budget || {};
    const chips = [];
    Object.entries(budget.limits || {}).forEach(([k, v]) => {
      chips.push(h("span", { className: "ancor-chip", key: "l-" + k }, esc(k) + ": " + esc(v)));
    });
    Object.entries(budget.allowed || {}).forEach(([k, vals]) => {
      if (Array.isArray(vals) && vals.length) {
        chips.push(h("span", { className: "ancor-chip ancor-chip-allow", key: "a-" + k },
          esc(k) + ": " + vals.slice(0, 2).join(", ") + (vals.length > 2 ? "…" : "")));
      }
    });
    const route = props.route || {};
    if (route.category) chips.push(h("span", { className: "ancor-chip ancor-chip-model", key: "cat" }, "category: " + esc(route.category)));
    const models = (route.observed || []).map((m) => m && m.model).filter(Boolean);
    if (models.length) {
      chips.push(h("span", { className: "ancor-chip ancor-chip-model", key: "models" }, "model: " + models.join(" → ")));
      if (new Set(models).size > 1) chips.push(h("span", { className: "ancor-chip ancor-chip-warn", key: "fb" }, "fallback"));
    }
    if (props.pausedBy) {
      chips.push(h("span", { className: "ancor-chip ancor-chip-warn", key: "paused" }, "paused by " + esc(props.pausedBy)));
    }
    return chips.length ? h("div", { className: "ancor-chips" }, chips) : null;
  }

  function GoalTree(props) {
    const goals = props.goals || [];
    if (!goals.length) return h("p", { className: "ancor-muted" }, "暂无活跃 Goal 节点");
    const byParent = {};
    const ids = {};
    goals.forEach((g) => {
      ids[g.goal_id] = true;
      const pid = g.parent_goal_id || "root";
      (byParent[pid] = byParent[pid] || []).push(g);
    });
    const roots = goals.filter((g) => !g.parent_goal_id || g.parent_goal_id === "root" || !ids[g.parent_goal_id]);
    function renderNode(node) {
      const children = byParent[node.goal_id] || [];
      const constraints = ((node.budget || {}).constraints) || [];
      const handles = node.external_job_ids || [];
      return h("div", { className: "ancor-goal-node ancor-st-" + esc(node.status), key: node.goal_id },
        h("div", { className: "ancor-goal-head" },
          h("button", { className: "ancor-goal-btn", onClick: () => props.onOpen && props.onOpen("goals", node.goal_id) }, esc(node.goal_id)),
          h("span", { className: "ancor-badge" }, esc(node.status))),
        h("div", { className: "ancor-goal-obj" }, esc(node.objective)),
        h(BudgetChips, { budget: node.budget, pausedBy: node.paused_by_ancestor,
          route: { category: node.category, observed: node.observed_models } }),
        handles.length ? h("div", { className: "ancor-chips" }, handles.map((j) =>
          h("span", { className: "ancor-chip ancor-chip-job", key: j.handle_id || j.job_id },
            esc(j.handle_id || j.job_id) + " (" + esc(j.status) + ")"))) : null,
        constraints.length ? h("ul", { className: "ancor-constraints" }, constraints.map((c, idx) =>
          h("li", { key: idx }, esc(c)))) : null,
        children.length ? h("div", { className: "ancor-goal-children" }, children.map(renderNode)) : null);
    }
    return h("div", { className: "ancor-goal-tree" }, roots.map(renderNode));
  }

  function List(props) {
    const items = props.items || [];
    return h("div", { className: "ancor-list" }, items.length ? items.map((item) => {
      const id = item.id || item.goal_id;
      const label = item.title || item.objective || item.kind || id;
      return h("article", { className: "ancor-item", key: id },
        h("button", { onClick: () => props.onOpen(item.detail_kind || props.kind, id) }, esc(label)),
        h("span", { className: "ancor-badge" }, esc(item.status || item.state || "")),
        item.generation != null ? h("small", null, "版本 gen " + item.generation) : null);
    }) : h("p", { className: "ancor-muted" }, "暂无数据"));
  }

  function Detail(props) {
    const detail = props.value;
    if (!detail) return null;
    const req = detail.request;
    if (!req) {
      return h("section", { className: "ancor-card ancor-detail" },
        h(Button, { onClick: props.onClose }, "关闭详情"),
        h("pre", { style: { whiteSpace: "pre-wrap" } }, JSON.stringify(detail, null, 2)));
    }
    const labels = detail.field_labels || { outcome: "目标", constraint: "约束", advice: "建议", context: "背景" };
    return h("section", { className: "ancor-card ancor-detail" },
      h(Button, { onClick: props.onClose }, "关闭详情"),
      h("h2", null, req.title),
      h("p", { className: "ancor-muted" }, "状态：" + req.status + " · 请求版本：gen " + req.generation),
      req.content_mode === "legacy" ? h("pre", { style: { whiteSpace: "pre-wrap" } }, req.brief) :
        Object.entries(detail.fields || {}).map(([key, points]) => h("section", { key },
          h("h3", null, labels[key] || key),
          h("ul", null, points.map((point) => h("li", { key: point.version_id }, point.body))))),
      h("h3", null, "执行树 (Goal Tree)"),
      h(GoalTree, { goals: detail.goals || [], onOpen: props.onOpen }),
      h("h3", null, "Judge 核验记录"),
      (detail.judge_verdicts || []).length ? h("ul", null, detail.judge_verdicts.map((v) => {
        const judgeModels = (v.runtime || []).map((m) => m && m.model).filter(Boolean);
        const produced = new Set();
        (detail.goals || []).forEach((g) => (g.observed_models || []).forEach((m) => m && m.model && produced.add(m.model)));
        const shared = judgeModels.filter((m) => produced.has(m));
        return h("li", { key: v.id }, esc(v.decision) + " (gen " + v.generation + ") — " + esc(v.summary),
          judgeModels.length ? h("span", { className: "ancor-chip ancor-chip-model" }, "judge model: " + judgeModels.join(", ")) : null,
          shared.length ? h("span", { className: "ancor-chip ancor-chip-warn" }, "与执行方同模型: " + shared.join(", ")) : null);
      })) :
        h("p", { className: "ancor-muted" }, "暂无 Judge 记录"),
      h("h3", null, "问题"),
      (detail.questions || []).length ? h("ul", null, detail.questions.map((q) =>
        h("li", { key: q.id }, q.body + " · " + q.state))) : h("p", { className: "ancor-muted" }, "暂无问题"),
      h("h3", null, "改动历史"),
      h("ul", null, ((detail.changes || {}).items || []).map((c) => h("li", { key: c.id },
        h("span", null, c.actor + " · " + new Date(c.committed_at * 1000).toLocaleString()),
        h("pre", { style: { whiteSpace: "pre-wrap" } }, JSON.stringify(c.operations, null, 2))))));
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

    const openItem = (kind, id) => setSelected({ kind, id });
    const goalsSection = (data && (data.goals || data.tasks)) || { count: 0, items: [] };
    const sections = data ? [
      ["请求", data.requests, "requests"],
      ["Goal 树", goalsSection, "goals"],
      ["待回答问题", data.questions, "notifications"],
      ["事件", data.events, "events"],
    ] : [];

    return h("section", { className: "ancor-app" },
      h("div", { className: "ancor-header" },
        h("div", null, h("h1", null, "ANCOR"), h("p", { className: "ancor-muted" }, "只读权威需求、递归 Goal 树与 Judge 核验状态")),
        h(Button, { className: "ancor-refresh", onClick: load }, "刷新")),
      h("div", { className: "ancor-cards" }, sections.map(([label, value]) =>
        h("div", { className: "ancor-card", key: label }, h("span", null, label), h("b", null, value.count)))),
      h(Detail, { value: detail, onClose: () => setSelected(null), onOpen: openItem }),
      h("div", { className: "ancor-grid" }, sections.map(([label, value, kind]) =>
        h("section", { key: label },
          h("h2", null, label),
          kind === "goals"
            ? h(GoalTree, { goals: value.items, onOpen: openItem })
            : h(List, { items: value.items, kind, onOpen: openItem })))),
      h("p", { className: "ancor-status", "aria-live": "polite" }, error ? "加载失败：" + error : "已更新 · 每 5 秒轮询 · 只读"));
  }
  registry.register("ancor", AncorPage);
})();
