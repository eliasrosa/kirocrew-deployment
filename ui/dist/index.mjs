import { jsxs as r, jsx as i } from "react/jsx-runtime";
import { useAppApi as E } from "@kirocrew/app-sdk";
import { PageHeader as T, Btn as w, Card as D } from "@kirocrew/app-sdk/ui";
import { useState as g, useCallback as _, useEffect as j } from "react";
function K(e) {
  return e < 60 ? `${e}m` : e < 1440 ? `${Math.floor(e / 60)}h` : `${Math.floor(e / 1440)}d`;
}
function P(e) {
  return e.split("/").pop() ?? e;
}
function S() {
  return {
    briefing: [],
    planning_specs: [],
    planning_review: [],
    develop_waiting: [],
    develop_running: [],
    review_waiting: [],
    review_approved: [],
    review_refused: [],
    qa_waiting: [],
    qa_testing: [],
    qa_approved: [],
    qa_refused: [],
    done: [],
    blocked: []
  };
}
const M = {
  squad_name: "KiroCrew Flow (demo)",
  project: "eliasrosa/kirocrew-flow",
  columns: {
    briefing: [
      { number: 95, title: "Exemplo: demanda sendo especificada", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 240, labels: ["flow:briefing"], blocked: !1, running: !1 }
    ],
    planning_specs: [
      { number: 97, title: "Exemplo: dev montando spec/critérios", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 120, labels: ["flow:planning-specs"], blocked: !1, running: !1 }
    ],
    planning_review: [],
    develop_waiting: [
      { number: 99, title: "Exemplo: aguardando agente", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 45, labels: ["flow:develop-waiting"], blocked: !1, running: !1 }
    ],
    develop_running: [
      { number: 100, title: "Exemplo: agente implementando", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 120, labels: ["flow:develop-running"], blocked: !1, running: !0 }
    ],
    review_waiting: [
      { number: 101, title: "Exemplo: PR aguardando reviewer", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 30, labels: ["flow:review-waiting"], blocked: !1, running: !1 }
    ],
    review_approved: [
      { number: 103, title: "Exemplo: reviewer aprovou — aguardando merge", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 15, labels: ["flow:review-approved"], blocked: !1, running: !1 }
    ],
    review_refused: [],
    qa_waiting: [],
    qa_testing: [],
    qa_approved: [],
    qa_refused: [],
    done: [],
    blocked: [
      { number: 102, title: "Exemplo: issue bloqueada", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 240, labels: ["flow:blocked"], blocked: !0, running: !1 }
    ]
  }
};
function B({ issue: e, showDispatch: n, onDispatch: t, dispatching: s, showQaButtons: m, onQaFail: f, onQaApprove: h, qaActioning: p }) {
  return /* @__PURE__ */ r(D, { style: { marginBottom: 8, padding: "10px 12px" }, children: [
    /* @__PURE__ */ r("div", { style: { display: "flex", alignItems: "flex-start", gap: 8 }, children: [
      /* @__PURE__ */ r("div", { style: { flex: 1, minWidth: 0 }, children: [
        /* @__PURE__ */ r("div", { style: { display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }, children: [
          /* @__PURE__ */ r("span", { style: { fontWeight: 600, fontSize: 12, opacity: 0.6, whiteSpace: "nowrap" }, children: [
            "#",
            e.number
          ] }),
          /* @__PURE__ */ i("span", { style: { fontWeight: 500, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, children: e.title })
        ] }),
        /* @__PURE__ */ r("div", { style: { display: "flex", alignItems: "center", gap: 8, fontSize: 11, opacity: 0.6 }, children: [
          /* @__PURE__ */ i("span", { children: P(e.repo) }),
          e.age_min > 0 && /* @__PURE__ */ r("span", { children: [
            "⏱ ",
            K(e.age_min)
          ] }),
          e.running && /* @__PURE__ */ i("span", { style: { color: "#f97316" }, children: "● running" })
        ] })
      ] }),
      n && t && /* @__PURE__ */ i(
        w,
        {
          size: "sm",
          variant: "secondary",
          disabled: s,
          onClick: () => t(e.repo, e.number),
          style: { flexShrink: 0, fontSize: 11 },
          children: s ? "..." : "Dispatch"
        }
      ),
      m && (h || f) && /* @__PURE__ */ r("div", { style: { display: "flex", flexDirection: "column", gap: 4, flexShrink: 0 }, children: [
        h && /* @__PURE__ */ i(
          w,
          {
            size: "sm",
            variant: "primary",
            disabled: !!p,
            onClick: () => h(e.repo, e.number),
            style: { fontSize: 11, background: "#22c55e", borderColor: "#16a34a" },
            children: p === "approve" ? "..." : "✓ Aprovar"
          }
        ),
        f && /* @__PURE__ */ i(
          w,
          {
            size: "sm",
            variant: "secondary",
            disabled: !!p,
            onClick: () => f(e.repo, e.number),
            style: { fontSize: 11, borderColor: "#9333ea", color: "#9333ea" },
            children: p === "fail" ? "..." : "✗ Reprovar"
          }
        )
      ] })
    ] }),
    e.url && /* @__PURE__ */ i(
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
function o({ title: e, issues: n, color: t, showDispatch: s, onDispatch: m, dispatchingKey: f, showQaButtons: h, onQaFail: p, onQaApprove: k, qaActioningKey: u }) {
  return /* @__PURE__ */ r("div", { style: { flex: 1, minWidth: 160 }, children: [
    /* @__PURE__ */ r("div", { style: {
      fontSize: 11,
      fontWeight: 600,
      textTransform: "uppercase",
      letterSpacing: "0.06em",
      opacity: 0.55,
      marginBottom: 8,
      paddingBottom: 4,
      borderBottom: `1px solid ${t}44`,
      display: "flex",
      alignItems: "center",
      gap: 6
    }, children: [
      e,
      /* @__PURE__ */ i("span", { style: {
        background: t + "33",
        color: t,
        borderRadius: 8,
        padding: "0 6px",
        fontSize: 10,
        fontWeight: 700
      }, children: n.length })
    ] }),
    n.length === 0 ? /* @__PURE__ */ i("div", { style: { fontSize: 12, opacity: 0.3, textAlign: "center", padding: "12px 0" }, children: "—" }) : n.map((v) => {
      const b = `${v.repo}#${v.number}`;
      return /* @__PURE__ */ i(
        B,
        {
          issue: v,
          showDispatch: s,
          onDispatch: m,
          dispatching: f === b,
          showQaButtons: h,
          onQaFail: p,
          onQaApprove: k,
          qaActioning: u != null && u.startsWith(`${b}:`) ? u.split(":").pop() : void 0
        },
        b
      );
    })
  ] });
}
function N({ columns: e, accentColor: n }) {
  const t = e.briefing.length + e.planning_specs.length + e.planning_review.length;
  return /* @__PURE__ */ r("div", { style: {
    border: `1px solid ${n}33`,
    borderRadius: 10,
    padding: "14px 16px",
    background: `${n}08`,
    flex: 1,
    minWidth: 260
  }, children: [
    /* @__PURE__ */ r("div", { style: { marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }, children: [
      /* @__PURE__ */ i("div", { style: { fontWeight: 700, fontSize: 13, color: n }, children: "Planning" }),
      /* @__PURE__ */ i("span", { style: {
        background: n + "33",
        color: n,
        borderRadius: 10,
        padding: "1px 7px",
        fontSize: 11,
        fontWeight: 700
      }, children: t })
    ] }),
    /* @__PURE__ */ r("div", { style: { display: "flex", gap: 12, flexWrap: "wrap" }, children: [
      /* @__PURE__ */ i(o, { title: "Briefing", issues: e.briefing, color: "#f59e0b" }),
      /* @__PURE__ */ i(o, { title: "Specs", issues: e.planning_specs, color: "#fbbf24" }),
      /* @__PURE__ */ i(o, { title: "Revisão TL", issues: e.planning_review, color: "#d97706" })
    ] })
  ] });
}
function F({ columns: e, onDispatch: n, dispatchingKey: t }) {
  const s = e.review_refused.length + e.qa_refused.length;
  return /* @__PURE__ */ r("div", { style: {
    border: "1px solid rgba(128,128,128,0.15)",
    borderRadius: 10,
    padding: "14px 16px",
    marginTop: 16
  }, children: [
    /* @__PURE__ */ i("div", { style: { fontWeight: 700, fontSize: 12, opacity: 0.4, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 14 }, children: "Agentes" }),
    /* @__PURE__ */ r("div", { style: { display: "flex", gap: 20, flexWrap: "wrap" }, children: [
      /* @__PURE__ */ r("div", { style: { flex: 1, minWidth: 300 }, children: [
        /* @__PURE__ */ r("div", { style: {
          fontWeight: 700,
          fontSize: 13,
          marginBottom: 12,
          paddingBottom: 6,
          borderBottom: "2px solid #2563eb",
          display: "flex",
          alignItems: "center",
          gap: 8
        }, children: [
          /* @__PURE__ */ i("span", { children: "Desenvolvimento" }),
          /* @__PURE__ */ i("span", { style: {
            background: "#2563eb",
            color: "#fff",
            borderRadius: 10,
            padding: "1px 7px",
            fontSize: 11,
            fontWeight: 700
          }, children: e.develop_waiting.length + e.develop_running.length })
        ] }),
        /* @__PURE__ */ r("div", { style: { display: "flex", gap: 12 }, children: [
          /* @__PURE__ */ i(
            o,
            {
              title: "Aguardando",
              issues: e.develop_waiting,
              color: "#16a34a",
              showDispatch: !0,
              onDispatch: n,
              dispatchingKey: t ?? void 0
            }
          ),
          /* @__PURE__ */ i(
            o,
            {
              title: "Implementando",
              issues: e.develop_running,
              color: "#2563eb"
            }
          )
        ] })
      ] }),
      /* @__PURE__ */ r("div", { style: { flex: 1, minWidth: 300 }, children: [
        /* @__PURE__ */ r("div", { style: {
          fontWeight: 700,
          fontSize: 13,
          marginBottom: 12,
          paddingBottom: 6,
          borderBottom: "2px solid #8b5cf6",
          display: "flex",
          alignItems: "center",
          gap: 8
        }, children: [
          /* @__PURE__ */ i("span", { children: "Code Review" }),
          /* @__PURE__ */ i("span", { style: {
            background: "#8b5cf6",
            color: "#fff",
            borderRadius: 10,
            padding: "1px 7px",
            fontSize: 11,
            fontWeight: 700
          }, children: e.review_waiting.length + e.review_approved.length })
        ] }),
        /* @__PURE__ */ r("div", { style: { display: "flex", gap: 12 }, children: [
          /* @__PURE__ */ i(
            o,
            {
              title: "Aguardando",
              issues: e.review_waiting,
              color: "#8b5cf6"
            }
          ),
          /* @__PURE__ */ i(
            o,
            {
              title: "Aprovado ✓",
              issues: e.review_approved,
              color: "#22c55e"
            }
          )
        ] })
      ] }),
      /* @__PURE__ */ r("div", { style: { flex: 1, minWidth: 300 }, children: [
        /* @__PURE__ */ r("div", { style: {
          fontWeight: 700,
          fontSize: 13,
          marginBottom: 12,
          paddingBottom: 6,
          borderBottom: "2px solid #0ea5e9",
          display: "flex",
          alignItems: "center",
          gap: 8
        }, children: [
          /* @__PURE__ */ i("span", { children: "QA" }),
          /* @__PURE__ */ i("span", { style: {
            background: "#0ea5e9",
            color: "#fff",
            borderRadius: 10,
            padding: "1px 7px",
            fontSize: 11,
            fontWeight: 700
          }, children: e.qa_waiting.length + e.qa_testing.length + e.qa_approved.length })
        ] }),
        /* @__PURE__ */ r("div", { style: { display: "flex", gap: 12 }, children: [
          /* @__PURE__ */ i(o, { title: "Aguardando", issues: e.qa_waiting, color: "#0ea5e9" }),
          /* @__PURE__ */ i(
            o,
            {
              title: "Testando",
              issues: e.qa_testing,
              color: "#38bdf8",
              showQaButtons: !0,
              onQaFail: handleQaFail,
              onQaApprove: handleQaApprove,
              qaActioningKey: qaActioningKey ?? void 0
            }
          ),
          /* @__PURE__ */ i(o, { title: "Aprovado ✓", issues: e.qa_approved, color: "#22c55e" })
        ] })
      ] })
    ] }),
    s > 0 && /* @__PURE__ */ r("div", { style: {
      marginTop: 16,
      border: "1px solid #9333ea33",
      borderRadius: 8,
      padding: "10px 14px",
      background: "#9333ea08"
    }, children: [
      /* @__PURE__ */ i("div", { style: { fontWeight: 700, fontSize: 12, color: "#9333ea", marginBottom: 8 }, children: "🚫 Gates humanos — aguardando decisão manual do TL/dev" }),
      /* @__PURE__ */ r("div", { style: { display: "flex", gap: 12, flexWrap: "wrap" }, children: [
        e.review_refused.length > 0 && /* @__PURE__ */ i(o, { title: "Review reprovado", issues: e.review_refused, color: "#9333ea" }),
        e.qa_refused.length > 0 && /* @__PURE__ */ i(o, { title: "QA reprovado", issues: e.qa_refused, color: "#9333ea" })
      ] })
    ] })
  ] });
}
function H() {
  const e = E(), [n, t] = g(S()), [s, m] = g("KiroCrew Flow"), [f, h] = g(""), [p, k] = g(!0), [u, v] = g(null), [b, z] = g(!1), [W, $] = g(null), [C, q] = g(null), [L, x] = g(null), d = _(() => e.get("/api/apps/kirocrew-flow/issues").then((a) => {
    t(a.columns ?? S()), m(a.squad_name || "KiroCrew Flow"), h(a.project || ""), $(/* @__PURE__ */ new Date()), v(null), z(!1);
  }).catch((a) => {
    const l = String(a);
    if (l.includes("404") || l.includes("not found")) {
      const c = M;
      t(c.columns ?? S()), m(c.squad_name), h(c.project), z(!0), v(null);
    } else
      v(l);
  }).finally(() => {
    k(!1);
  }), [e]);
  j(() => {
    d();
    const a = setInterval(d, 15e3);
    return () => clearInterval(a);
  }, [d]);
  const R = _(
    async (a, l) => {
      const c = `${a}#${l}`;
      q(c);
      try {
        await e.post("/api/apps/kirocrew-flow/dispatch", { repo: a, number: Number(l) }), await d();
      } catch (y) {
        console.error("dispatch failed:", y);
      } finally {
        q(null);
      }
    },
    [e, d]
  );
  _(
    async (a, l) => {
      const c = `${a}#${l}:fail`;
      x(c);
      try {
        await e.post("/api/apps/kirocrew-flow/qa-fail", { repo: a, number: Number(l) }), await d();
      } catch (y) {
        console.error("qa-fail failed:", y);
      } finally {
        x(null);
      }
    },
    [e, d]
  ), _(
    async (a, l) => {
      const c = `${a}#${l}:approve`;
      x(c);
      try {
        await e.post("/api/apps/kirocrew-flow/qa-approve", { repo: a, number: Number(l) }), await d();
      } catch (y) {
        console.error("qa-approve failed:", y);
      } finally {
        x(null);
      }
    },
    [e, d]
  );
  const A = f ? `Flow - ${s} / ${f.split("/").pop()}` : s, I = n.briefing.length + n.planning_specs.length + n.planning_review.length + n.develop_waiting.length + n.develop_running.length + n.review_waiting.length + n.review_approved.length + n.review_refused.length + n.qa_waiting.length + n.qa_testing.length + n.qa_approved.length + n.qa_refused.length + n.blocked.length;
  return /* @__PURE__ */ r("div", { style: { padding: "20px 24px", maxWidth: 1400 }, children: [
    /* @__PURE__ */ i(
      T,
      {
        title: A,
        subtitle: p ? "Carregando…" : u ? `Erro: ${u}` : b ? "⚠️ Modo demo — backend indisponível" : W ? `${I} issues ativas · atualizado ${W.toLocaleTimeString()}` : "",
        actions: /* @__PURE__ */ r("div", { style: { display: "flex", gap: 8 }, children: [
          /* @__PURE__ */ i(w, { size: "sm", variant: "secondary", disabled: !0, children: "Rules" }),
          /* @__PURE__ */ i(w, { size: "sm", variant: "secondary", disabled: !0, children: "Configurações" }),
          /* @__PURE__ */ i(w, { size: "sm", variant: "secondary", onClick: d, disabled: p, children: "↻ Atualizar" })
        ] })
      }
    ),
    /* @__PURE__ */ i("div", { style: { display: "flex", gap: 16, marginTop: 20, flexWrap: "wrap" }, children: /* @__PURE__ */ i(N, { columns: n, accentColor: "#f59e0b" }) }),
    /* @__PURE__ */ i(
      F,
      {
        columns: n,
        onDispatch: R,
        dispatchingKey: C
      }
    ),
    n.blocked.length > 0 && /* @__PURE__ */ r("div", { style: {
      marginTop: 20,
      border: "1px solid #dc262633",
      borderRadius: 10,
      padding: "14px 16px",
      background: "#dc262608"
    }, children: [
      /* @__PURE__ */ r("div", { style: {
        fontWeight: 700,
        fontSize: 13,
        color: "#dc2626",
        marginBottom: 10,
        display: "flex",
        alignItems: "center",
        gap: 8
      }, children: [
        "🔴 Bloqueadas",
        /* @__PURE__ */ i("span", { style: {
          background: "#dc2626",
          color: "#fff",
          borderRadius: 10,
          padding: "1px 7px",
          fontSize: 11
        }, children: n.blocked.length })
      ] }),
      /* @__PURE__ */ i("div", { style: { display: "flex", flexWrap: "wrap", gap: 8 }, children: n.blocked.map((a) => /* @__PURE__ */ i("div", { style: { minWidth: 200, flex: "0 0 auto", maxWidth: 280 }, children: /* @__PURE__ */ i(B, { issue: a }) }, `${a.repo}#${a.number}`)) })
    ] })
  ] });
}
export {
  H as default
};
