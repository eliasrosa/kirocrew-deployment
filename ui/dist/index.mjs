import { jsxs as t, jsx as r } from "react/jsx-runtime";
import { useAppApi as T } from "@kirocrew/app-sdk";
import { PageHeader as P, Btn as h, Card as j } from "@kirocrew/app-sdk/ui";
import { useState as d, useCallback as z, useEffect as q } from "react";
function D(e) {
  return e < 60 ? `${e}m` : e < 1440 ? `${Math.floor(e / 60)}h` : `${Math.floor(e / 1440)}d`;
}
function K(e) {
  return e.split("/").pop() ?? e;
}
function y() {
  return {
    spec: [],
    ready: [],
    todo: [],
    dev: [],
    review: [],
    review_ok: [],
    reviewed: [],
    done: [],
    blocked: []
  };
}
const M = {
  squad_name: "KiroCrew Flow (demo)",
  project: "eliasrosa/kirocrew-flow",
  columns: {
    spec: [
      { number: 95, title: "Exemplo: feature sendo especificada", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 240, labels: ["crewflow:spec", "crewflow:feature"], blocked: !1, running: !1 }
    ],
    ready: [
      { number: 97, title: "Exemplo: spec pronta, aguardando priorização", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 120, labels: ["crewflow:ready", "crewflow:feature"], blocked: !1, running: !1 }
    ],
    todo: [
      { number: 99, title: "Exemplo: feature aguardando dev", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 45, labels: ["crewflow:feature", "crewflow:p2"], blocked: !1, running: !1 }
    ],
    dev: [
      { number: 100, title: "Exemplo: issue em implementação", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 120, labels: ["crewflow:bug", "crewflow:p1"], blocked: !1, running: !0 }
    ],
    review: [
      { number: 101, title: "Exemplo: PR aguardando review", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 30, labels: ["crewflow:feature"], blocked: !1, running: !1 }
    ],
    review_ok: [
      { number: 103, title: "Exemplo: PR aprovado, aguardando merge", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 15, labels: ["crewflow:feature", "crewflow:review-ok"], blocked: !1, running: !1 }
    ],
    reviewed: [],
    done: [],
    blocked: [
      { number: 102, title: "Exemplo: issue bloqueada", repo: "eliasrosa/kirocrew-flow", url: "", age_min: 240, labels: ["crewflow:debt"], blocked: !0, running: !1 }
    ]
  }
};
function b({ issue: e, showDispatch: i, onDispatch: o, dispatching: n }) {
  return /* @__PURE__ */ t(j, { style: { marginBottom: 8, padding: "10px 12px" }, children: [
    /* @__PURE__ */ t("div", { style: { display: "flex", alignItems: "flex-start", gap: 8 }, children: [
      /* @__PURE__ */ t("div", { style: { flex: 1, minWidth: 0 }, children: [
        /* @__PURE__ */ t("div", { style: { display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }, children: [
          /* @__PURE__ */ t("span", { style: { fontWeight: 600, fontSize: 12, opacity: 0.6, whiteSpace: "nowrap" }, children: [
            "#",
            e.number
          ] }),
          /* @__PURE__ */ r("span", { style: { fontWeight: 500, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, children: e.title })
        ] }),
        /* @__PURE__ */ t("div", { style: { display: "flex", alignItems: "center", gap: 8, fontSize: 11, opacity: 0.6 }, children: [
          /* @__PURE__ */ r("span", { children: K(e.repo) }),
          e.age_min > 0 && /* @__PURE__ */ t("span", { children: [
            "⏱ ",
            D(e.age_min)
          ] }),
          e.running && /* @__PURE__ */ r("span", { style: { color: "#f97316" }, children: "● running" })
        ] })
      ] }),
      i && o && /* @__PURE__ */ r(
        h,
        {
          size: "sm",
          variant: "secondary",
          disabled: n,
          onClick: () => o(e.repo, e.number),
          style: { flexShrink: 0, fontSize: 11 },
          children: n ? "..." : "Dispatch"
        }
      )
    ] }),
    e.url && /* @__PURE__ */ r(
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
function m({ title: e, issues: i, color: o, showDispatch: n, onDispatch: a, dispatchingKey: u }) {
  return /* @__PURE__ */ t("div", { style: { flex: 1, minWidth: 160 }, children: [
    /* @__PURE__ */ t("div", { style: {
      fontSize: 11,
      fontWeight: 600,
      textTransform: "uppercase",
      letterSpacing: "0.06em",
      opacity: 0.55,
      marginBottom: 8,
      paddingBottom: 4,
      borderBottom: `1px solid ${o}44`,
      display: "flex",
      alignItems: "center",
      gap: 6
    }, children: [
      e,
      /* @__PURE__ */ r("span", { style: {
        background: o + "33",
        color: o,
        borderRadius: 8,
        padding: "0 6px",
        fontSize: 10,
        fontWeight: 700
      }, children: i.length })
    ] }),
    i.length === 0 ? /* @__PURE__ */ r("div", { style: { fontSize: 12, opacity: 0.3, textAlign: "center", padding: "12px 0" }, children: "—" }) : i.map((s) => {
      const f = `${s.repo}#${s.number}`;
      return /* @__PURE__ */ r(
        b,
        {
          issue: s,
          showDispatch: n,
          onDispatch: a,
          dispatching: u === f
        },
        f
      );
    })
  ] });
}
function W({ title: e, subtitle: i, issues: o, accentColor: n }) {
  return /* @__PURE__ */ t("div", { style: {
    flex: 1,
    border: `1px solid ${n}33`,
    borderRadius: 10,
    padding: "14px 16px",
    background: `${n}08`,
    minWidth: 220
  }, children: [
    /* @__PURE__ */ t("div", { style: { marginBottom: 10 }, children: [
      /* @__PURE__ */ r("div", { style: { fontWeight: 700, fontSize: 13, color: n, marginBottom: 2 }, children: e }),
      /* @__PURE__ */ r("div", { style: { fontSize: 11, opacity: 0.5 }, children: i })
    ] }),
    o.length === 0 ? /* @__PURE__ */ r("div", { style: { fontSize: 12, opacity: 0.3, textAlign: "center", padding: "12px 0" }, children: "—" }) : o.map((a) => /* @__PURE__ */ r(b, { issue: a }, `${a.repo}#${a.number}`))
  ] });
}
function F({ columns: e, onDispatch: i, dispatchingKey: o }) {
  return /* @__PURE__ */ t("div", { style: {
    border: "1px solid rgba(128,128,128,0.15)",
    borderRadius: 10,
    padding: "14px 16px",
    marginTop: 16
  }, children: [
    /* @__PURE__ */ r("div", { style: { fontWeight: 700, fontSize: 12, opacity: 0.4, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 14 }, children: "Agentes" }),
    /* @__PURE__ */ t("div", { style: { display: "flex", gap: 20, flexWrap: "wrap" }, children: [
      /* @__PURE__ */ t("div", { style: { flex: 1, minWidth: 300 }, children: [
        /* @__PURE__ */ t("div", { style: {
          fontWeight: 700,
          fontSize: 13,
          marginBottom: 12,
          paddingBottom: 6,
          borderBottom: "2px solid #2563eb",
          display: "flex",
          alignItems: "center",
          gap: 8
        }, children: [
          /* @__PURE__ */ r("span", { children: "Desenvolvimento" }),
          /* @__PURE__ */ r("span", { style: {
            background: "#2563eb",
            color: "#fff",
            borderRadius: 10,
            padding: "1px 7px",
            fontSize: 11,
            fontWeight: 700
          }, children: e.todo.length + e.dev.length })
        ] }),
        /* @__PURE__ */ t("div", { style: { display: "flex", gap: 12 }, children: [
          /* @__PURE__ */ r(
            m,
            {
              title: "Aguardando",
              issues: e.todo,
              color: "#16a34a",
              showDispatch: !0,
              onDispatch: i,
              dispatchingKey: o ?? void 0
            }
          ),
          /* @__PURE__ */ r(
            m,
            {
              title: "Trabalhando",
              issues: e.dev,
              color: "#2563eb"
            }
          )
        ] })
      ] }),
      /* @__PURE__ */ t("div", { style: { flex: 1, minWidth: 300 }, children: [
        /* @__PURE__ */ t("div", { style: {
          fontWeight: 700,
          fontSize: 13,
          marginBottom: 12,
          paddingBottom: 6,
          borderBottom: "2px solid #8b5cf6",
          display: "flex",
          alignItems: "center",
          gap: 8
        }, children: [
          /* @__PURE__ */ r("span", { children: "Code Review" }),
          /* @__PURE__ */ r("span", { style: {
            background: "#8b5cf6",
            color: "#fff",
            borderRadius: 10,
            padding: "1px 7px",
            fontSize: 11,
            fontWeight: 700
          }, children: e.review.length + e.review_ok.length })
        ] }),
        /* @__PURE__ */ t("div", { style: { display: "flex", gap: 12 }, children: [
          /* @__PURE__ */ r(
            m,
            {
              title: "Aguardando",
              issues: e.review,
              color: "#8b5cf6"
            }
          ),
          /* @__PURE__ */ r(
            m,
            {
              title: "Aprovado ✓",
              issues: e.review_ok,
              color: "#22c55e"
            }
          )
        ] })
      ] })
    ] })
  ] });
}
function H() {
  const e = T(), [i, o] = d(y()), [n, a] = d("KiroCrew Flow"), [u, s] = d(""), [f, _] = d(!0), [x, w] = d(null), [$, v] = d(!1), [k, B] = d(null), [C, S] = d(null), c = z(() => e.get("/api/apps/kirocrew-flow/issues").then((l) => {
    o(l.columns ?? y()), a(l.squad_name || "KiroCrew Flow"), s(l.project || ""), B(/* @__PURE__ */ new Date()), w(null), v(!1);
  }).catch((l) => {
    const p = String(l);
    if (p.includes("404") || p.includes("not found")) {
      const g = M;
      o(g.columns ?? y()), a(g.squad_name), s(g.project), v(!0), w(null);
    } else
      w(p);
  }).finally(() => {
    _(!1);
  }), [e]);
  q(() => {
    c();
    const l = setInterval(c, 15e3);
    return () => clearInterval(l);
  }, [c]);
  const A = z(
    async (l, p) => {
      const g = `${l}#${p}`;
      S(g);
      try {
        await e.post("/api/apps/kirocrew-flow/dispatch", { repo: l, number: Number(p) }), await c();
      } catch (I) {
        console.error("dispatch failed:", I);
      } finally {
        S(null);
      }
    },
    [e, c]
  ), E = u ? `Flow - ${n} / ${u.split("/").pop()}` : n, R = i.spec.length + i.ready.length + i.todo.length + i.dev.length + i.review.length + i.review_ok.length + i.blocked.length;
  return /* @__PURE__ */ t("div", { style: { padding: "20px 24px", maxWidth: 1400 }, children: [
    /* @__PURE__ */ r(
      P,
      {
        title: E,
        subtitle: f ? "Carregando…" : x ? `Erro: ${x}` : $ ? "⚠️ Modo demo — backend indisponível" : k ? `${R} issues ativas · atualizado ${k.toLocaleTimeString()}` : "",
        actions: /* @__PURE__ */ t("div", { style: { display: "flex", gap: 8 }, children: [
          /* @__PURE__ */ r(h, { size: "sm", variant: "secondary", disabled: !0, children: "Rules" }),
          /* @__PURE__ */ r(h, { size: "sm", variant: "secondary", disabled: !0, children: "Configurações" }),
          /* @__PURE__ */ r(h, { size: "sm", variant: "secondary", onClick: c, disabled: f, children: "↻ Atualizar" })
        ] })
      }
    ),
    /* @__PURE__ */ t("div", { style: { display: "flex", gap: 16, marginTop: 20, flexWrap: "wrap" }, children: [
      /* @__PURE__ */ r(
        W,
        {
          title: "Aguardando SPEC",
          subtitle: "PM especificando",
          issues: i.spec,
          accentColor: "#f59e0b"
        }
      ),
      /* @__PURE__ */ r(
        W,
        {
          title: "Aguardando definição de produto/TL",
          subtitle: "Spec pronta, aguardando priorização",
          issues: i.ready,
          accentColor: "#fbbf24"
        }
      )
    ] }),
    /* @__PURE__ */ r(
      F,
      {
        columns: i,
        onDispatch: A,
        dispatchingKey: C
      }
    ),
    i.blocked.length > 0 && /* @__PURE__ */ t("div", { style: {
      marginTop: 20,
      border: "1px solid #dc262633",
      borderRadius: 10,
      padding: "14px 16px",
      background: "#dc262608"
    }, children: [
      /* @__PURE__ */ t("div", { style: {
        fontWeight: 700,
        fontSize: 13,
        color: "#dc2626",
        marginBottom: 10,
        display: "flex",
        alignItems: "center",
        gap: 8
      }, children: [
        "🔴 Bloqueadas",
        /* @__PURE__ */ r("span", { style: {
          background: "#dc2626",
          color: "#fff",
          borderRadius: 10,
          padding: "1px 7px",
          fontSize: 11
        }, children: i.blocked.length })
      ] }),
      /* @__PURE__ */ r("div", { style: { display: "flex", flexWrap: "wrap", gap: 8 }, children: i.blocked.map((l) => /* @__PURE__ */ r("div", { style: { minWidth: 200, flex: "0 0 auto", maxWidth: 280 }, children: /* @__PURE__ */ r(b, { issue: l }) }, `${l.repo}#${l.number}`)) })
    ] })
  ] });
}
export {
  H as default
};
