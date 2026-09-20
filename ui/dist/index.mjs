import { jsxs as r, jsx as t } from "react/jsx-runtime";
import { useAppApi as C } from "@kirocrew/app-sdk";
import { PageHeader as $, Btn as w, Card as B } from "@kirocrew/app-sdk/ui";
import { useState as s, useCallback as u, useEffect as W } from "react";
function A(e) {
  return e < 60 ? `${e}m` : e < 1440 ? `${Math.floor(e / 60)}h` : `${Math.floor(e / 1440)}d`;
}
function D(e) {
  return e.split("/").pop() ?? e;
}
function I({ issue: e, showDispatch: n, onDispatch: i, dispatching: l }) {
  return /* @__PURE__ */ r(B, { style: { marginBottom: 8, padding: "10px 12px" }, children: [
    /* @__PURE__ */ r("div", { style: { display: "flex", alignItems: "flex-start", gap: 8 }, children: [
      /* @__PURE__ */ r("div", { style: { flex: 1, minWidth: 0 }, children: [
        /* @__PURE__ */ r("div", { style: { display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }, children: [
          /* @__PURE__ */ r("span", { style: { fontWeight: 600, fontSize: 12, opacity: 0.6, whiteSpace: "nowrap" }, children: [
            "#",
            e.number
          ] }),
          /* @__PURE__ */ t("span", { style: { fontWeight: 500, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, children: e.title })
        ] }),
        /* @__PURE__ */ r("div", { style: { display: "flex", alignItems: "center", gap: 8, fontSize: 11, opacity: 0.6 }, children: [
          /* @__PURE__ */ t("span", { children: D(e.repo) }),
          e.age_min > 0 && /* @__PURE__ */ r("span", { children: [
            "⏱ ",
            A(e.age_min)
          ] }),
          e.running && /* @__PURE__ */ t("span", { style: { color: "#f97316" }, children: "● running" })
        ] })
      ] }),
      n && i && /* @__PURE__ */ t(
        w,
        {
          size: "sm",
          variant: "secondary",
          disabled: l,
          onClick: () => i(e.repo, e.number),
          style: { flexShrink: 0, fontSize: 11 },
          children: l ? "..." : "Dispatch"
        }
      )
    ] }),
    e.url && /* @__PURE__ */ t(
      "a",
      {
        href: e.url,
        target: "_blank",
        rel: "noreferrer",
        style: { fontSize: 11, opacity: 0.5, textDecoration: "none", display: "block", marginTop: 4 },
        children: "🔗 Ver issue"
      }
    )
  ] });
}
function v({ title: e, issues: n, color: i, showDispatch: l, onDispatch: g, dispatchingKey: p }) {
  return /* @__PURE__ */ r("div", { style: { flex: 1, minWidth: 180, maxWidth: 260 }, children: [
    /* @__PURE__ */ r(
      "div",
      {
        style: {
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 10,
          paddingBottom: 6,
          borderBottom: `2px solid ${i}`
        },
        children: [
          /* @__PURE__ */ t("span", { style: { fontWeight: 700, fontSize: 13, textTransform: "uppercase", letterSpacing: "0.04em" }, children: e }),
          /* @__PURE__ */ t(
            "span",
            {
              style: {
                background: i,
                color: "#fff",
                borderRadius: 10,
                padding: "1px 7px",
                fontSize: 11,
                fontWeight: 700
              },
              children: n.length
            }
          )
        ]
      }
    ),
    n.length === 0 ? /* @__PURE__ */ t("div", { style: { fontSize: 12, opacity: 0.4, textAlign: "center", padding: "16px 0" }, children: "—" }) : n.map((a) => {
      const c = `${a.repo}#${a.number}`;
      return /* @__PURE__ */ t(
        I,
        {
          issue: a,
          showDispatch: l,
          onDispatch: g,
          dispatching: p === c
        },
        c
      );
    })
  ] });
}
function _() {
  const e = C(), [n, i] = s({
    todo: [],
    dev: [],
    review: [],
    reviewed: [],
    done: [],
    blocked: []
  }), [l, g] = s(!0), [p, a] = s(null), [c, x] = s(null), [k, y] = s(null), d = u(() => e.get("/api/apps/kirocrew-flow/issues").then((o) => {
    i(o.columns ?? { todo: [], dev: [], review: [], reviewed: [], done: [], blocked: [] }), x(/* @__PURE__ */ new Date()), a(null);
  }).catch((o) => {
    a(String(o));
  }).finally(() => {
    g(!1);
  }), [e]);
  W(() => {
    d();
    const o = setInterval(d, 15e3);
    return () => clearInterval(o);
  }, [d]);
  const b = u(
    async (o, h) => {
      const m = `${o}#${h}`;
      y(m);
      try {
        await e.post("/api/apps/kirocrew-flow/dispatch", { repo: o, number: Number(h) }), await d();
      } catch (f) {
        console.error("dispatch failed:", f);
      } finally {
        y(null);
      }
    },
    [e, d]
  ), S = [
    { key: "todo", title: "Todo", color: "#16a34a", showDispatch: !0 },
    { key: "dev", title: "Dev", color: "#2563eb" },
    { key: "review", title: "Review", color: "#8b5cf6" },
    { key: "reviewed", title: "QA", color: "#0ea5e9" },
    { key: "done", title: "Done", color: "#22c55e" }
  ], z = n.todo.length + n.dev.length + n.review.length + n.reviewed.length + n.blocked.length;
  return /* @__PURE__ */ r("div", { style: { padding: "20px 24px", maxWidth: 1400 }, children: [
    /* @__PURE__ */ t(
      $,
      {
        title: "KiroCrew Flow",
        subtitle: l ? "Carregando…" : p ? `Erro: ${p}` : c ? `${z} issues ativas · atualizado ${c.toLocaleTimeString()}` : "",
        actions: /* @__PURE__ */ t(w, { size: "sm", variant: "secondary", onClick: d, disabled: l, children: "↻ Atualizar" })
      }
    ),
    /* @__PURE__ */ t(
      "div",
      {
        style: {
          display: "flex",
          gap: 16,
          overflowX: "auto",
          marginTop: 20,
          paddingBottom: 8
        },
        children: S.map(({ key: o, title: h, color: m, showDispatch: f }) => /* @__PURE__ */ t(
          v,
          {
            title: h,
            issues: n[o],
            color: m,
            showDispatch: f,
            onDispatch: f ? b : void 0,
            dispatchingKey: k ?? void 0
          },
          o
        ))
      }
    ),
    n.blocked.length > 0 && /* @__PURE__ */ t("div", { style: { marginTop: 24 }, children: /* @__PURE__ */ t(
      v,
      {
        title: "Blocked",
        issues: n.blocked,
        color: "#dc2626"
      }
    ) })
  ] });
}
export {
  _ as default
};
