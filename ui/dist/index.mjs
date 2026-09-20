import { jsxs as n, jsx as t } from "react/jsx-runtime";
import { useAppApi as B } from "@kirocrew/app-sdk";
import { PageHeader as I, Btn as k, Card as W } from "@kirocrew/app-sdk/ui";
import { useState as c, useCallback as v, useEffect as _ } from "react";
function A(e) {
  return e < 60 ? `${e}m` : e < 1440 ? `${Math.floor(e / 60)}h` : `${Math.floor(e / 1440)}d`;
}
function D(e) {
  return e.split("/").pop() ?? e;
}
function E({ issue: e, showDispatch: l, onDispatch: r, dispatching: a }) {
  return /* @__PURE__ */ n(W, { style: { marginBottom: 8, padding: "10px 12px" }, children: [
    /* @__PURE__ */ n("div", { style: { display: "flex", alignItems: "flex-start", gap: 8 }, children: [
      /* @__PURE__ */ n("div", { style: { flex: 1, minWidth: 0 }, children: [
        /* @__PURE__ */ n("div", { style: { display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }, children: [
          /* @__PURE__ */ n("span", { style: { fontWeight: 600, fontSize: 12, opacity: 0.6, whiteSpace: "nowrap" }, children: [
            "#",
            e.number
          ] }),
          /* @__PURE__ */ t("span", { style: { fontWeight: 500, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, children: e.title })
        ] }),
        /* @__PURE__ */ n("div", { style: { display: "flex", alignItems: "center", gap: 8, fontSize: 11, opacity: 0.6 }, children: [
          /* @__PURE__ */ t("span", { children: D(e.repo) }),
          e.age_min > 0 && /* @__PURE__ */ n("span", { children: [
            "⏱ ",
            A(e.age_min)
          ] }),
          e.running && /* @__PURE__ */ t("span", { style: { color: "#f97316" }, children: "● running" })
        ] })
      ] }),
      l && r && /* @__PURE__ */ t(
        k,
        {
          size: "sm",
          variant: "secondary",
          disabled: a,
          onClick: () => r(e.repo, e.number),
          style: { flexShrink: 0, fontSize: 11 },
          children: a ? "..." : "Dispatch"
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
function b({ title: e, issues: l, color: r, showDispatch: a, onDispatch: u, dispatchingKey: p }) {
  return /* @__PURE__ */ n("div", { style: { flex: 1, minWidth: 180, maxWidth: 260 }, children: [
    /* @__PURE__ */ n(
      "div",
      {
        style: {
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 10,
          paddingBottom: 6,
          borderBottom: `2px solid ${r}`
        },
        children: [
          /* @__PURE__ */ t("span", { style: { fontWeight: 700, fontSize: 13, textTransform: "uppercase", letterSpacing: "0.04em" }, children: e }),
          /* @__PURE__ */ t(
            "span",
            {
              style: {
                background: r,
                color: "#fff",
                borderRadius: 10,
                padding: "1px 7px",
                fontSize: 11,
                fontWeight: 700
              },
              children: l.length
            }
          )
        ]
      }
    ),
    l.length === 0 ? /* @__PURE__ */ t("div", { style: { fontSize: 12, opacity: 0.4, textAlign: "center", padding: "16px 0" }, children: "—" }) : l.map((d) => {
      const f = `${d.repo}#${d.number}`;
      return /* @__PURE__ */ t(
        E,
        {
          issue: d,
          showDispatch: a,
          onDispatch: u,
          dispatching: p === f
        },
        f
      );
    })
  ] });
}
const M = {
  todo: [
    { number: 99, title: "Exemplo: feature aguardando dev", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 45, labels: ["crewflow:feature", "crewflow:p2"], blocked: !1, running: !1 }
  ],
  dev: [
    { number: 100, title: "Exemplo: issue em implementação", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 120, labels: ["crewflow:bug", "crewflow:p1"], blocked: !1, running: !0 }
  ],
  review: [
    { number: 101, title: "Exemplo: PR aguardando review", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 30, labels: ["crewflow:feature"], blocked: !1, running: !1 }
  ],
  reviewed: [],
  done: [],
  blocked: [
    { number: 102, title: "Exemplo: issue bloqueada", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 240, labels: ["crewflow:debt"], blocked: !0, running: !1 }
  ]
};
function R() {
  const e = B(), [l, r] = c({
    todo: [],
    dev: [],
    review: [],
    reviewed: [],
    done: [],
    blocked: []
  }), [a, u] = c(!0), [p, d] = c(null), [f, h] = c(!1), [w, x] = c(null), [S, y] = c(null), s = v(() => e.get("/api/apps/kirocrew-flow/issues").then((o) => {
    r(o.columns ?? { todo: [], dev: [], review: [], reviewed: [], done: [], blocked: [] }), x(/* @__PURE__ */ new Date()), d(null), h(!1);
  }).catch((o) => {
    const i = String(o);
    i.includes("404") || i.includes("not found") ? (r(M), h(!0), d(null)) : d(i);
  }).finally(() => {
    u(!1);
  }), [e]);
  _(() => {
    s();
    const o = setInterval(s, 15e3);
    return () => clearInterval(o);
  }, [s]);
  const C = v(
    async (o, i) => {
      const m = `${o}#${i}`;
      y(m);
      try {
        await e.post("/api/apps/kirocrew-flow/dispatch", { repo: o, number: Number(i) }), await s();
      } catch (g) {
        console.error("dispatch failed:", g);
      } finally {
        y(null);
      }
    },
    [e, s]
  ), z = [
    { key: "todo", title: "Todo", color: "#16a34a", showDispatch: !0 },
    { key: "dev", title: "Dev", color: "#2563eb" },
    { key: "review", title: "Review", color: "#8b5cf6" },
    { key: "reviewed", title: "QA", color: "#0ea5e9" },
    { key: "done", title: "Done", color: "#22c55e" }
  ], $ = l.todo.length + l.dev.length + l.review.length + l.reviewed.length + l.blocked.length;
  return /* @__PURE__ */ n("div", { style: { padding: "20px 24px", maxWidth: 1400 }, children: [
    /* @__PURE__ */ t(
      I,
      {
        title: "KiroCrew Flow",
        subtitle: a ? "Carregando…" : p ? `Erro: ${p}` : f ? "⚠️ Modo demo — backend indisponível (issue #170)" : w ? `${$} issues ativas · atualizado ${w.toLocaleTimeString()}` : "",
        actions: /* @__PURE__ */ t(k, { size: "sm", variant: "secondary", onClick: s, disabled: a, children: "↻ Atualizar" })
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
        children: z.map(({ key: o, title: i, color: m, showDispatch: g }) => /* @__PURE__ */ t(
          b,
          {
            title: i,
            issues: l[o],
            color: m,
            showDispatch: g,
            onDispatch: g ? C : void 0,
            dispatchingKey: S ?? void 0
          },
          o
        ))
      }
    ),
    l.blocked.length > 0 && /* @__PURE__ */ t("div", { style: { marginTop: 24 }, children: /* @__PURE__ */ t(
      b,
      {
        title: "Blocked",
        issues: l.blocked,
        color: "#dc2626"
      }
    ) })
  ] });
}
export {
  R as default
};
