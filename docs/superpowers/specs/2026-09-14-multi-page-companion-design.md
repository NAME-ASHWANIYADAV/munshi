# Multi-page companion screen — design

**Date:** 2026-09-14
**Status:** approved, ready for implementation planning

## Problem

The companion screen is one dense dashboard. Asked what was wrong with it, the answer was that
everything is squeezed — not that the product looks small, and not that it is hard to navigate.
That distinction sets the whole design: the fix is *room*, not more surface area for its own sake.

The squeeze is measurable. The memory graph renders a 200-node knowledge graph inside a box roughly
330×150 CSS pixels, so it had to be culled to 26 nodes to stay legible at all. The merchant-health
signal — five weighted dimensions, each with its own evidence sentence, and the strongest answer the
project has to "why would Paytm care" — is reachable only by hovering a chip in the header, which on
a projector is not reachable at all. Insight cards truncate their own headlines. The compliance
refusals (who was not contacted, and under which rule) are returned by the API on every action and
have never been displayed anywhere.

## Non-goals

- Making the product *look* bigger. Pages exist where something is currently cramped, and nowhere else.
- A component library or CSS framework. The project has `react` and `react-dom` as its only runtime dependencies and keeps them.
- Per-page data fetching. The store already loads everything on mount; navigation must be instant during a demo.
- Rewriting the existing panels. They gain a `variant` prop; their internals are left alone.

## Architecture

### Routing

A hand-rolled router in `src/router.tsx`, around seventy lines, built on the History API. No routing
dependency: the project's two runtime dependencies are a deliberate property, and a five-route app
does not need a general routing engine.

Paths, not hashes (`/sehat`, not `/#/sehat`) — the URL is visible during a demo and a hash reads as a
prototype. The cost is that a hosted static site returns 404 on a deep-link refresh, so `vercel.json`
gains a rewrite sending every path to `index.html`.

It exposes:

- `useRoute()` — the current pathname, and a `navigate(path)` that pushes history
- `<Link to="…">` — an anchor that intercepts plain left-clicks and lets modified clicks through, so a judge can still open a page in a new tab
- An unknown path renders the call page rather than an error, because a broken URL during a demo should land somewhere useful

### Shell

`App.tsx` becomes: `<Header />`, `<LedgerTabs />`, the routed page, and — on every page except the
call page — a persistent `<CallStrip />`.

The strip is the load-bearing decision. This product's thesis is that it is a *conversation*; a page
where the conversation disappears argues against the product. On the call page the conversation is
the full panel (scrollback, tool trace, suggestion chips). Everywhere else it is a strip at the foot
of the screen carrying the last two turns and the mic, so the presenter can keep talking while moving
between rooms and a viewer never loses the thread.

### Navigation

`LedgerTabs` renders the bahi khata's index tabs: tabs attached to the top edge of the page surface,
the active one joined to it with no dividing border, the way a real register's section tabs work.
Each carries a Devanagari name with a small English label beneath.

This is the one structural device the design spends its boldness on, and it earns its place twice: it
is true to the subject, and reading the tabs tells a judge what the product does before they click
anything.

## Pages

| Route | Name | What it gets that the panel could not have |
|---|---|---|
| `/` | बात-चीत · The call | The conversation and today's figures side by side, both with room to breathe |
| `/salah` | सलाह · Advice | Each finding as a full card: its metrics, its confidence, the rule that found it, the action it suggests |
| `/kaam` | काम · Actions | The approval queue, the history with measured outcomes, and **the refusals** — who was not contacted and under which rule |
| `/yaaddasht` | याददाश्त · Memory | The graph full-bleed with more nodes, a node inspector, and the recall path drawn across it |
| `/sehat` | सेहत · Health | Five dimensions, each with a score bar, its evidence, its weight and its contribution to the composite |

`/kaam` and `/sehat` display data the API already returns and the UI has never shown. They are the
two pages that add capability rather than only space.

## Components

Existing panels gain a `variant` prop rather than being split or duplicated:

- `LiveCall` — `full` | `strip`
- `InsightFeed` — `rail` | `page`
- `ActionQueue` — `rail` | `page`
- `MemoryGraph` — `panel` | `page` (the page variant raises the node cap from 26 to 90 and labels up to 20 rather than 6; the force simulation is untouched)

New:

- `router.tsx` — routing primitives
- `LedgerTabs.tsx` — the navigation
- `CallStrip.tsx` — the compact conversation, wrapping `LiveCall` in its strip variant
- `HealthDial.tsx` — the composite score, its band, and its movement since last month
- `DimensionBar.tsx` — one dimension: score bar, evidence line, weight, contribution

Changed:

- `Header.tsx` — the health chip stops being a `title` tooltip and becomes a link to `/sehat`

### Styling

A new `styles/pages.css` holds page layouts and the tab navigation. `tokens.css` is not touched: the
palette, type scale and spacing scale are already right, and the user explicitly said the colour
theme works. Page layouts use the existing `--s-*` spacing and `--t-*` type scales throughout.

## Data flow

Unchanged. `AppProvider` already fetches the dashboard, merchant health, insights, actions and graph
on mount and keeps them live over SSE. Pages read from `useApp()` and render; none of them fetch.

This is deliberate. The payloads are small, they are already in memory, and a demo in which clicking
a tab shows a spinner is worse than one where it does not.

## Error and empty states

Each page follows the pattern already used by the panels: when its slice of the store is still
`null`, it renders the existing loading treatment; when the slice is present but empty, it renders a
sentence saying what would put something there. No page renders a bare empty region.

The offline fixture path is unaffected — `VITE_USE_FIXTURES=auto` continues to serve every page from
`fixtures.ts` when the API cannot be reached, so all five routes work with no backend running.

## Verification

There is no frontend test suite; the 656 tests in this project are backend and unaffected by this
work. Verification is therefore explicit:

1. `npx tsc --noEmit` clean
2. `npm run build` clean
3. All five routes loaded in a real browser at 1440×900, in **both** light and dark themes
4. A deep link (`/sehat`) refreshed directly, to prove the Vercel rewrite works
5. The call strip present and functional on all four non-home pages
6. Keyboard focus visible on the tabs, and `prefers-reduced-motion` respected on any tab transition

## Risks

**The tabs could read as decoration.** Mitigated by making them the only bold structural element and
keeping every page surface quiet underneath.

**Five pages could leave some feeling thin.** `/salah` is the one at risk, since the insight feed is
already reasonably complete. If it looks thin when built, the fix is to show the statistics behind
each finding — the baseline, the z-score, the rule — which the API already returns in `metrics` and
which no view currently displays.

**A deep-link refresh 404s** if the Vercel rewrite is forgotten. It is in the verification list for
that reason.
