# Multi-page Companion Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the MunshiJi companion screen from one dense dashboard into five routed pages, so that every panel that is currently squeezed gets its own room.

**Architecture:** A hand-rolled History-API router (no routing dependency) drives five pages. The existing panels are reused via a `variant` prop rather than split or duplicated. A persistent call strip keeps the conversation visible on every page except the call page itself. Navigation is rendered as a bahi khata's index tabs.

**Tech Stack:** React 18, TypeScript, Vite. Runtime dependencies stay exactly `react` and `react-dom`.

## Global Constraints

- **No new runtime dependencies.** `frontend/package.json` `dependencies` must remain `react` and `react-dom` only.
- **Do not modify `frontend/src/styles/tokens.css`.** The palette, `--t-*` type scale and `--s-*` spacing scale are approved and final. All new CSS consumes those tokens; no new literal colours.
- **Both themes must work.** Every new colour comes from a token so light and dark both resolve. Never define a colour only inside a media or `[data-theme]` block.
- **Paths, not hashes.** Routes are `/`, `/salah`, `/kaam`, `/yaaddasht`, `/sehat`.
- **Hindi is content, not decoration.** Devanagari strings use `className="deva"` and `lang="hi"`.
- **Verification is tsc + build + browser**, not unit tests. This frontend has no test suite and this plan does not add one — the project's 656 tests are backend and untouched.
- Every task ends with `npx tsc --noEmit` clean, `npm run build` clean, and a commit.

---

### Task 0: Make the working tree a git repository

The project root is not a repo, so the per-task commits in this plan have nowhere to go.

**Files:**
- Create: `C:\Users\HP\OneDrive\Desktop\paytm\.git` (via `git init`)

- [ ] **Step 1: Initialise and make the first commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git init -q
git add -A
git -c commit.gpgsign=false commit -q -m "MunshiJi at the single-page baseline

Snapshot before the companion screen is split into pages, so the change is reviewable as a diff."
git log --oneline
```

Expected: one commit listed. `.gitignore` already exists and excludes `node_modules/`, `.venv/`, `data/*.db` and `.env`.

---

### Task 1: Router

**Files:**
- Create: `frontend/src/router.tsx`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ROUTES: readonly RouteDef[]` where `RouteDef = { path: string; nameHi: string; nameEn: string }`
  - `useRoute(): { path: string; navigate: (to: string) => void }`
  - `<Link to="/sehat" className="…">…</Link>`
  - `<RouterProvider>{children}</RouterProvider>`

- [ ] **Step 1: Write the router**

```tsx
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type ReactNode,
} from 'react'

/**
 * A five-route app does not need a routing engine.
 *
 * The project keeps `react` and `react-dom` as its only runtime dependencies, which is a
 * deliberate property rather than an accident, and everything below fits in one screen of code.
 * Paths rather than hashes, because the URL is visible during a demo and `/#/sehat` reads as a
 * prototype — the cost is a rewrite rule on the host, which `vercel.json` carries.
 */

export interface RouteDef {
  path: string
  /** What the tab says. */
  nameHi: string
  /** The gloss underneath it, for anyone who does not read Devanagari. */
  nameEn: string
}

export const ROUTES: readonly RouteDef[] = [
  { path: '/', nameHi: 'बात-चीत', nameEn: 'the call' },
  { path: '/salah', nameHi: 'सलाह', nameEn: 'advice' },
  { path: '/kaam', nameHi: 'काम', nameEn: 'actions' },
  { path: '/yaaddasht', nameHi: 'याददाश्त', nameEn: 'memory' },
  { path: '/sehat', nameHi: 'सेहत', nameEn: 'health' },
]

interface RouterValue {
  path: string
  navigate: (to: string) => void
}

const RouterContext = createContext<RouterValue | null>(null)

/** Trailing slashes are stripped so `/sehat/` and `/sehat` are the same page. */
function normalise(path: string): string {
  if (path.length > 1 && path.endsWith('/')) return path.slice(0, -1)
  return path || '/'
}

export function RouterProvider({ children }: { children: ReactNode }): JSX.Element {
  const [path, setPath] = useState(() => normalise(window.location.pathname))

  useEffect(() => {
    const onPop = (): void => setPath(normalise(window.location.pathname))
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const navigate = useCallback((to: string) => {
    const next = normalise(to)
    if (next === normalise(window.location.pathname)) return
    window.history.pushState(null, '', next)
    setPath(next)
  }, [])

  const value = useMemo(() => ({ path, navigate }), [path, navigate])
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

export function useRoute(): RouterValue {
  const value = useContext(RouterContext)
  if (value === null) throw new Error('useRoute must be used inside <RouterProvider>')
  return value
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }

/**
 * Intercepts a plain left-click and lets every modified click through, so middle-click and
 * ctrl-click still open a page in a new tab — a judge poking around should not be trapped.
 */
export function Link({ to, onClick, children, ...rest }: LinkProps): JSX.Element {
  const { navigate } = useRoute()
  return (
    <a
      href={to}
      onClick={(event) => {
        onClick?.(event)
        if (event.defaultPrevented) return
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
          return
        }
        event.preventDefault()
        navigate(to)
      }}
      {...rest}
    >
      {children}
    </a>
  )
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd /c/Users/HP/OneDrive/Desktop/paytm/frontend && npx tsc --noEmit`
Expected: no output (the file is not imported yet, but it must type-check).

- [ ] **Step 3: Commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git add frontend/src/router.tsx
git -c commit.gpgsign=false commit -q -m "Add a hand-rolled router

Five routes do not justify a routing dependency, and the project keeps react and react-dom as
its only runtime dependencies on purpose."
```

---

### Task 2: Ledger tabs and the page shell

**Files:**
- Create: `frontend/src/components/LedgerTabs.tsx`
- Create: `frontend/src/styles/pages.css`
- Modify: `frontend/src/styles/index.css` (add the import)
- Modify: `frontend/src/App.tsx` (whole file replaced)

**Interfaces:**
- Consumes: `ROUTES`, `useRoute`, `Link` from `../router`.
- Produces: `<LedgerTabs />`; the CSS classes `.tabs`, `.tab`, `.tab--on`, `.page`, `.page__head`, `.page__title`, `.page__lede`, `.page__body`, `.shell--paged`.

- [ ] **Step 1: Write the tabs**

```tsx
import { Link, ROUTES, useRoute } from '../router'

/**
 * The index tabs down the edge of a bahi khata.
 *
 * The active tab joins the page surface with no dividing border, the way a real register's
 * section tab does. This is the one bold structural element in the design, and it earns its
 * place twice: it is true to the subject, and reading the tabs tells a visitor what the product
 * does before they click anything.
 */
export function LedgerTabs(): JSX.Element {
  const { path } = useRoute()
  return (
    <nav className="tabs" aria-label="Sections">
      {ROUTES.map((route) => {
        const active = route.path === path
        return (
          <Link
            key={route.path}
            to={route.path}
            className={active ? 'tab tab--on' : 'tab'}
            aria-current={active ? 'page' : undefined}
          >
            <span className="tab__hi deva" lang="hi">
              {route.nameHi}
            </span>
            <span className="tab__en">{route.nameEn}</span>
          </Link>
        )
      })}
    </nav>
  )
}
```

- [ ] **Step 2: Write the page CSS**

Create `frontend/src/styles/pages.css`:

```css
/* Page shell, ledger tabs, and the layouts each page uses. Tokens only — see tokens.css. */

.shell--paged {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  padding: 12px 16px 14px;
  gap: var(--s-2);
}

/* ================================================================ ledger tabs */

.tabs {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  padding-inline: var(--s-4);
  /* Sits on the page surface's top edge; the active tab erases this line beneath itself. */
  margin-bottom: -1px;
  overflow-x: auto;
  scrollbar-width: none;
}

.tabs::-webkit-scrollbar {
  display: none;
}

.tab {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: var(--s-2) var(--s-4) 7px;
  border: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
  border-radius: var(--r-md) var(--r-md) 0 0;
  background: var(--panel-sunk);
  color: var(--ink-3);
  text-decoration: none;
  white-space: nowrap;
  transition:
    background var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}

.tab:hover {
  background: var(--panel-inset);
  color: var(--ink-2);
}

.tab:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: -2px;
}

/* Joined to the page below it, and marked by the khata's red. */
.tab--on {
  background: var(--panel);
  border-bottom-color: var(--panel);
  border-top: 3px solid var(--red);
  padding-top: calc(var(--s-2) - 2px);
  color: var(--ink);
}

.tab__hi {
  font-size: var(--t-md);
  font-weight: 600;
  line-height: 1.2;
}

.tab__en {
  font-size: var(--t-label);
  letter-spacing: var(--track-label);
  text-transform: uppercase;
  color: var(--ink-3);
}

.tab--on .tab__en {
  color: var(--red-ink);
}

/* ===================================================================== page */

.page {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border: 1px solid var(--rule);
  border-radius: 0 var(--r-lg) var(--r-lg) var(--r-lg);
  box-shadow: var(--shadow-panel);
  overflow: hidden;
}

.page__head {
  display: flex;
  align-items: baseline;
  gap: var(--s-3);
  flex-wrap: wrap;
  padding: var(--s-4) var(--s-5) var(--s-3);
  border-bottom: 3px double var(--rule-strong);
}

.page__title {
  font-family: var(--font-display);
  font-size: var(--t-xl);
  font-weight: 700;
  letter-spacing: var(--track-tight);
  line-height: 1.1;
  margin: 0;
}

.page__lede {
  font-size: var(--t-base);
  color: var(--ink-3);
  margin: 0;
  max-width: 62ch;
}

.page__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--s-5);
}

@media (max-width: 880px) {
  .shell--paged {
    height: auto;
    min-height: 100dvh;
  }

  .page__body {
    padding: var(--s-4);
  }
}

@media (prefers-reduced-motion: reduce) {
  .tab {
    transition: none;
  }
}
```

- [ ] **Step 3: Import the stylesheet**

In `frontend/src/styles/index.css`, add after the existing imports:

```css
@import './pages.css';
```

- [ ] **Step 4: Replace App.tsx with the routed shell**

```tsx
import { Header } from './components/Header'
import { LedgerTabs } from './components/LedgerTabs'
import { ActionsPage } from './pages/ActionsPage'
import { AdvicePage } from './pages/AdvicePage'
import { CallPage } from './pages/CallPage'
import { HealthPage } from './pages/HealthPage'
import { MemoryPage } from './pages/MemoryPage'
import { useRoute } from './router'

/**
 * The shell: the shop's header, the ledger tabs, and whichever page they select.
 *
 * An unrecognised path falls through to the call page rather than an error screen — a mistyped
 * URL during a demo should land somewhere useful.
 */
export default function App(): JSX.Element {
  const { path } = useRoute()

  const page =
    path === '/salah' ? (
      <AdvicePage />
    ) : path === '/kaam' ? (
      <ActionsPage />
    ) : path === '/yaaddasht' ? (
      <MemoryPage />
    ) : path === '/sehat' ? (
      <HealthPage />
    ) : (
      <CallPage />
    )

  return (
    <div className="shell shell--paged">
      <Header />
      <LedgerTabs />
      {page}
    </div>
  )
}
```

- [ ] **Step 5: Wrap the app in the router**

Replace the render call in `frontend/src/main.tsx`. `RouterProvider` sits inside `AppProvider` so
a page can read both the store and the current route:

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import { RouterProvider } from './router'
import { AppProvider } from './state/store'
import './styles/index.css'

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <AppProvider>
      <RouterProvider>
        <App />
      </RouterProvider>
    </AppProvider>
  </StrictMode>,
)
```

If the existing file differs in how it imports `AppProvider` or the stylesheet, keep those lines
as they are and change only the nesting.

- [ ] **Step 6: Create the five page stubs so the shell compiles**

Create each of `frontend/src/pages/CallPage.tsx`, `AdvicePage.tsx`, `ActionsPage.tsx`, `MemoryPage.tsx`, `HealthPage.tsx` with this shape, substituting the name and copy (these are replaced by Tasks 3–7):

```tsx
export function CallPage(): JSX.Element {
  return (
    <main className="page">
      <header className="page__head">
        <h1 className="page__title">बात-चीत</h1>
        <p className="page__lede">The call.</p>
      </header>
      <div className="page__body">Replaced in a later task.</div>
    </main>
  )
}
```

- [ ] **Step 7: Verify**

Run: `cd /c/Users/HP/OneDrive/Desktop/paytm/frontend && npx tsc --noEmit && npm run build`
Expected: no type errors; build succeeds.

Then load `http://localhost:5173` and click every tab. Expected: the URL changes, the active tab joins the page surface, and the browser back button returns to the previous tab.

- [ ] **Step 8: Commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git add frontend/src
git -c commit.gpgsign=false commit -q -m "Route the shell through ledger tabs

The active tab joins the page surface with no dividing border, the way a register's section tab
does. Reading the tabs says what the product does before anything is clicked."
```

---

### Task 3: Call page, LiveCall variant, and the call strip

**Files:**
- Modify: `frontend/src/components/LiveCall.tsx` (add the prop; the component keeps its current body for `full`)
- Create: `frontend/src/components/CallStrip.tsx`
- Rewrite: `frontend/src/pages/CallPage.tsx`
- Modify: `frontend/src/App.tsx` (render the strip on non-call pages)
- Modify: `frontend/src/styles/pages.css` (append the strip styles)

**Interfaces:**
- Consumes: `useApp()` from `../state/store` — specifically `transcript`, `send`, `busy`.
- Produces: `<LiveCall variant="full" | "strip" />` (defaults to `"full"`, so existing usage is unaffected); `<CallStrip />`.

- [ ] **Step 1: Give LiveCall a variant prop**

At the top of the component signature in `frontend/src/components/LiveCall.tsx`:

```tsx
export interface LiveCallProps {
  /**
   * `full` is the call page: scrollback, tool trace and suggestion chips.
   * `strip` is every other page: the last two turns and the mic, nothing else.
   */
  variant?: 'full' | 'strip'
}

export function LiveCall({ variant = 'full' }: LiveCallProps): JSX.Element {
```

Inside, derive:

```tsx
const compact = variant === 'strip'
const shown = compact ? transcript.slice(-2) : transcript
```

Render the transcript from `shown`. When `compact` is true, do not render the suggestion chips, the tool-trace blocks, or the panel header; keep the input row and the mic. Add `compact ? 'livecall livecall--strip' : 'livecall'` as the root class.

- [ ] **Step 2: Write the strip wrapper**

```tsx
import { LiveCall } from './LiveCall'

/**
 * The conversation, kept present on every page that is not the call itself.
 *
 * This product's thesis is that it is a conversation; a page where the conversation disappears
 * quietly argues the opposite. Two turns and a mic is enough to keep the thread visible while
 * the presenter moves between rooms.
 */
export function CallStrip(): JSX.Element {
  return (
    <aside className="callstrip" aria-label="Live call">
      <LiveCall variant="strip" />
    </aside>
  )
}
```

- [ ] **Step 3: Write the call page**

```tsx
import { LiveCall } from '../components/LiveCall'
import { TodayNumbers } from '../components/TodayNumbers'

/** The conversation and today's figures, side by side, both with room. */
export function CallPage(): JSX.Element {
  return (
    <main className="page">
      <header className="page__head">
        <h1 className="page__title deva" lang="hi">
          बात-चीत
        </h1>
        <p className="page__lede">
          Ask in Hindi, Hinglish or English. Every figure spoken here was computed from the shop's
          own ledger.
        </p>
      </header>
      <div className="page__body page__body--call">
        <LiveCall variant="full" />
        <TodayNumbers />
      </div>
    </main>
  )
}
```

- [ ] **Step 4: Render the strip on non-call pages**

In `App.tsx`, after `{page}`:

```tsx
{path === '/' ? null : <CallStrip />}
```

- [ ] **Step 5: Append the CSS**

```css
/* ============================================================= call page + strip */

.page__body--call {
  display: grid;
  grid-template-columns: 1.25fr 1fr;
  gap: var(--s-4);
  align-items: start;
}

@media (max-width: 1100px) {
  .page__body--call {
    grid-template-columns: 1fr;
  }
}

/* The conversation, reduced to its thread. Fixed height so pages above it never reflow. */
.callstrip {
  flex: none;
  height: 96px;
  background: var(--panel);
  border: 1px solid var(--rule);
  border-top: 3px double var(--rule-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-panel);
  overflow: hidden;
}

.livecall--strip .turn__bubble {
  font-size: var(--t-base);
  padding: var(--s-2) var(--s-3);
}

.livecall--strip .turn__who {
  display: none;
}
```

- [ ] **Step 6: Verify**

Run: `npx tsc --noEmit && npm run build`
Then in the browser: on `/` the full call panel and today's numbers sit side by side; on `/salah` the strip is at the foot of the screen, shows the last two turns, and typing into it still works.

- [ ] **Step 7: Commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git add frontend/src
git -c commit.gpgsign=false commit -q -m "Keep the conversation present on every page

A page where the conversation disappears argues against the product. The call page keeps the
full panel; everywhere else carries the last two turns and the mic."
```

---

### Task 4: Health page

The highest-value page: the merchant-health signal is currently reachable only by hovering a chip, which on a projector is not reachable at all.

**Files:**
- Create: `frontend/src/components/DimensionBar.tsx`
- Create: `frontend/src/components/HealthDial.tsx`
- Rewrite: `frontend/src/pages/HealthPage.tsx`
- Modify: `frontend/src/components/Header.tsx` (chip becomes a link)
- Modify: `frontend/src/styles/pages.css` (append)

**Interfaces:**
- Consumes: `useApp().merchantHealth` of type `MerchantHealthOut` — fields `score`, `band`, `band_hi`, `previous_score`, `delta`, `weakest_dimension`, `dimensions[]`; each dimension has `key`, `label_en`, `label_hi`, `score`, `weight`, `contribution`, `evidence`, `reason_en`, `reason_hi`, `metrics`.
- Produces: `<HealthDial health={…} />`, `<DimensionBar dimension={…} weakest={boolean} />`.

- [ ] **Step 1: Write DimensionBar**

```tsx
import type { HealthDimensionOut } from '../api/types'

export interface DimensionBarProps {
  dimension: HealthDimensionOut
  /** The lowest-scoring dimension is marked, because it is what MunshiJi offers to work on. */
  weakest?: boolean
}

/**
 * One axis of the score, with the number behind it.
 *
 * The evidence line is not decoration: a score a credit officer cannot interrogate is one they
 * will not use, so every dimension shows what it measured and how much it counted for.
 */
export function DimensionBar({ dimension, weakest = false }: DimensionBarProps): JSX.Element {
  return (
    <article className={weakest ? 'dim dim--weakest' : 'dim'}>
      <div className="dim__head">
        <h3 className="dim__label">
          {dimension.label_en}
          <span className="dim__hi deva" lang="hi">
            {dimension.label_hi}
          </span>
        </h3>
        <div className="dim__score tabular">{dimension.score.toFixed(1)}</div>
      </div>

      <div
        className="dim__track"
        role="img"
        aria-label={`${dimension.label_en}: ${dimension.score.toFixed(0)} out of 100`}
      >
        <div className="dim__fill" style={{ width: `${Math.max(0, Math.min(100, dimension.score))}%` }} />
      </div>

      <p className="dim__evidence mono">{dimension.evidence}</p>
      <p className="dim__reason">{dimension.reason_en}</p>

      <div className="dim__weights">
        <span>
          weight <b className="tabular">{dimension.weight.toFixed(2)}</b>
        </span>
        <span>
          contributes <b className="tabular">{dimension.contribution.toFixed(1)}</b>
        </span>
        {weakest ? <span className="dim__flag">weakest</span> : null}
      </div>
    </article>
  )
}
```

- [ ] **Step 2: Write HealthDial**

```tsx
import type { MerchantHealthOut } from '../api/types'

/** The composite, its band, and which way it has moved since last month. */
export function HealthDial({ health }: { health: MerchantHealthOut }): JSX.Element {
  const delta = health.delta ?? 0
  return (
    <div className={`dial dial--${health.band}`}>
      <div className="dial__score tabular">{health.score.toFixed(1)}</div>
      <div className="dial__of">out of 100</div>
      <div className="dial__band deva" lang="hi">
        {health.band_hi}
        <span className="dial__band-en">{health.band}</span>
      </div>
      {health.previous_score === null ? null : (
        <div className={delta < 0 ? 'dial__delta dial__delta--down' : 'dial__delta dial__delta--up'}>
          {delta > 0 ? '+' : ''}
          {delta.toFixed(1)} <span>since last month</span>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 3: Write the page**

```tsx
import { DimensionBar } from '../components/DimensionBar'
import { HealthDial } from '../components/HealthDial'
import { useApp } from '../state/store'

/**
 * The lender-facing read of the same engines that advise the merchant.
 *
 * Paytm does not make its money selling dashboards to kirana owners; it makes it on payments,
 * subscriptions and distributing credit. A merchant who talks to MunshiJi every day is producing
 * the evidence that last question needs, as a by-product.
 */
export function HealthPage(): JSX.Element {
  const { merchantHealth } = useApp()

  return (
    <main className="page">
      <header className="page__head">
        <h1 className="page__title deva" lang="hi">
          सेहत
        </h1>
        <p className="page__lede">
          How the shop looks to someone deciding whether to lend to it — built from the same
          engines that advise the merchant. A signal, not a credit decision.
        </p>
      </header>

      <div className="page__body">
        {merchantHealth === null ? (
          <p className="empty">Working out the shop's health…</p>
        ) : (
          <div className="health">
            <HealthDial health={merchantHealth} />
            <div className="health__dims">
              {merchantHealth.dimensions.map((dimension) => (
                <DimensionBar
                  key={dimension.key}
                  dimension={dimension}
                  weakest={dimension.key === merchantHealth.weakest_dimension}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </main>
  )
}
```

- [ ] **Step 4: Make the header chip a link**

In `frontend/src/components/Header.tsx`, import `Link` from `../router`, remove the `title={…}` tooltip entirely, and change the chip's wrapper element from `<div className={...}>` to:

```tsx
<Link to="/sehat" className={`sehat sehat--${merchantHealth.band}`}>
```

closing with `</Link>`. Remove `cursor: default` from `.sehat` in `panels.css` and add `text-decoration: none;`.

- [ ] **Step 5: Append the CSS**

```css
/* ================================================================ health page */

.health {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: var(--s-5);
  align-items: start;
}

@media (max-width: 1000px) {
  .health {
    grid-template-columns: 1fr;
  }
}

.dial {
  padding: var(--s-5);
  border: 1px solid var(--rule-strong);
  border-left: 4px solid var(--gold);
  border-radius: var(--r-lg);
  background: var(--panel-inset);
  text-align: center;
  position: sticky;
  top: 0;
}

.dial--watch {
  border-left-color: var(--gold);
}

.dial--strained {
  border-left-color: var(--red);
}

.dial__score {
  font-family: var(--font-display);
  font-size: clamp(56px, 7vw, 84px);
  font-weight: 700;
  line-height: 1;
  letter-spacing: var(--track-tight);
  font-variant-numeric: tabular-nums lining-nums;
}

.dial__of {
  font-size: var(--t-label);
  letter-spacing: var(--track-label);
  text-transform: uppercase;
  color: var(--ink-3);
  margin-top: var(--s-1);
}

.dial__band {
  margin-top: var(--s-3);
  font-size: var(--t-lg);
  font-weight: 600;
  color: var(--gold-ink);
}

.dial__band-en {
  display: block;
  font-family: var(--font-mono);
  font-size: var(--t-label);
  letter-spacing: var(--track-label);
  text-transform: uppercase;
  color: var(--ink-3);
  margin-top: 2px;
}

.dial__delta {
  margin-top: var(--s-3);
  padding-top: var(--s-3);
  border-top: 1px solid var(--rule);
  font-family: var(--font-mono);
  font-size: var(--t-sm);
  font-weight: 600;
}

.dial__delta span {
  display: block;
  font-family: var(--font-text);
  font-weight: 400;
  color: var(--ink-3);
}

.dial__delta--down {
  color: var(--red-ink);
}

.dial__delta--up {
  color: var(--green-ink);
}

.health__dims {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
}

.dim {
  padding: var(--s-4);
  border: 1px solid var(--rule);
  border-radius: var(--r-md);
  background: var(--panel);
}

.dim--weakest {
  border-color: var(--red-edge);
  background: var(--red-wash);
}

.dim__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--s-3);
}

.dim__label {
  margin: 0;
  font-size: var(--t-md);
  font-weight: 600;
  display: flex;
  align-items: baseline;
  gap: var(--s-2);
}

.dim__hi {
  font-size: var(--t-sm);
  font-weight: 400;
  color: var(--ink-3);
}

.dim__score {
  font-family: var(--font-display);
  font-size: var(--t-xl);
  font-weight: 700;
  font-variant-numeric: tabular-nums lining-nums;
}

.dim__track {
  height: 6px;
  margin: var(--s-2) 0 var(--s-3);
  border-radius: 99px;
  background: var(--panel-sunk);
  overflow: hidden;
}

.dim__fill {
  height: 100%;
  background: var(--ink-2);
}

.dim--weakest .dim__fill {
  background: var(--red);
}

.dim__evidence {
  margin: 0 0 var(--s-1);
  font-size: var(--t-sm);
  color: var(--ink);
}

.dim__reason {
  margin: 0 0 var(--s-2);
  font-size: var(--t-sm);
  color: var(--ink-3);
  max-width: 68ch;
}

.dim__weights {
  display: flex;
  gap: var(--s-4);
  font-size: var(--t-label);
  letter-spacing: var(--track-label);
  text-transform: uppercase;
  color: var(--ink-3);
}

.dim__flag {
  color: var(--red-ink);
  font-weight: 600;
}
```

- [ ] **Step 6: Verify**

Run: `npx tsc --noEmit && npm run build`
Then load `/sehat` in both themes. Expected: the composite reads 84.4, five dimension cards, revenue trend marked as weakest with the red treatment, and every card showing its evidence, weight and contribution. Clicking the header chip navigates here.

- [ ] **Step 7: Commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git add frontend/src
git -c commit.gpgsign=false commit -q -m "Give the merchant-health signal its own page

It was reachable only by hovering a chip, which on a projector is not reachable at all. Each
dimension now shows what it measured, what it weighed and what it contributed, because a score a
credit officer cannot interrogate is one they will not use."
```

---

### Task 5: Actions page, with the refusals

**Files:**
- Modify: `frontend/src/components/ActionQueue.tsx` (variant prop)
- Rewrite: `frontend/src/pages/ActionsPage.tsx`
- Modify: `frontend/src/styles/pages.css` (append)

**Interfaces:**
- Consumes: `useApp().actions` (`ActionListOut`). Each `ActionOut` carries `result` (a `JsonObject`) which for screened actions contains `compliance: { refused_count, refusals: [{ name, rule, reason_en }], blocked, blocked_reason_en }`.
- Produces: `<ActionQueue variant="rail" | "page" />` (defaults `"rail"`).

- [ ] **Step 1: Give ActionQueue a variant prop**

```tsx
export interface ActionQueueProps {
  variant?: 'rail' | 'page'
}

export function ActionQueue({ variant = 'rail' }: ActionQueueProps): JSX.Element {
```

When `variant === 'page'`, render without the panel chrome (`.panel` wrapper and `.panel__head`) — the page supplies those — and drop the scroll container so the page body scrolls instead.

- [ ] **Step 2: Write the page, including the refusal list**

```tsx
import { ActionQueue } from '../components/ActionQueue'
import { useApp } from '../state/store'

interface Refusal {
  name: string
  rule: string
  reason_en: string
}

/** Pull the compliance refusals out of whatever actions carry them. */
function refusalsFrom(result: Record<string, unknown>): Refusal[] {
  const compliance = result.compliance
  if (typeof compliance !== 'object' || compliance === null) return []
  const list = (compliance as { refusals?: unknown }).refusals
  if (!Array.isArray(list)) return []
  return list.filter(
    (entry): entry is Refusal =>
      typeof entry === 'object' &&
      entry !== null &&
      typeof (entry as Refusal).name === 'string' &&
      typeof (entry as Refusal).rule === 'string',
  )
}

const RULE_LABEL: Record<string, string> = {
  no_marketing_consent: 'no marketing consent on file',
  opted_out: 'asked not to be contacted',
}

/**
 * What MunshiJi did, and what it declined to do.
 *
 * The refusals are the part no other view has ever shown. A guardrail nobody can see is
 * decoration, so every recipient that was dropped is named here with the rule that dropped them.
 */
export function ActionsPage(): JSX.Element {
  const { actions } = useApp()
  const refused = (actions?.actions ?? []).flatMap((action) =>
    refusalsFrom(action.result).map((refusal) => ({ ...refusal, actionId: action.id })),
  )

  return (
    <main className="page">
      <header className="page__head">
        <h1 className="page__title deva" lang="hi">
          काम
        </h1>
        <p className="page__lede">
          Nothing leaves without the merchant saying yes. Every send carries what it cost and what
          it is expected to bring back.
        </p>
      </header>

      <div className="page__body">
        <ActionQueue variant="page" />

        <section className="refusals">
          <h2 className="refusals__title">
            Not contacted
            <span className="refusals__count tabular">{refused.length}</span>
          </h2>
          <p className="refusals__lede">
            An offer is a marketing message and needs consent the customer actually gave. A
            reminder about someone's own balance does not, but an opt-out stops both.
          </p>
          {refused.length === 0 ? (
            <p className="empty">Nobody has been dropped from a send yet.</p>
          ) : (
            <ul className="refusals__list">
              {refused.map((refusal, index) => (
                <li className="refusal" key={`${refusal.actionId}-${index}`}>
                  <b>{refusal.name}</b>
                  <span className="refusal__rule">
                    {RULE_LABEL[refusal.rule] ?? refusal.rule.replace(/_/g, ' ')}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </main>
  )
}
```

- [ ] **Step 3: Append the CSS**

```css
/* =============================================================== actions page */

.refusals {
  margin-top: var(--s-6);
  padding-top: var(--s-4);
  border-top: 3px double var(--rule-strong);
}

.refusals__title {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  margin: 0 0 var(--s-1);
  font-family: var(--font-display);
  font-size: var(--t-lg);
  font-weight: 700;
}

.refusals__count {
  font-family: var(--font-mono);
  font-size: var(--t-sm);
  padding: 1px 8px;
  border-radius: 99px;
  background: var(--red-wash);
  color: var(--red-ink);
  border: 1px solid var(--red-edge);
}

.refusals__lede {
  margin: 0 0 var(--s-3);
  font-size: var(--t-sm);
  color: var(--ink-3);
  max-width: 68ch;
}

.refusals__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--s-2);
}

.refusal {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--s-2) var(--s-3);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--red);
  border-radius: var(--r-sm);
  background: var(--panel-inset);
}

.refusal__rule {
  font-size: var(--t-label);
  letter-spacing: var(--track-label);
  text-transform: uppercase;
  color: var(--ink-3);
}
```

- [ ] **Step 4: Verify**

Run: `npx tsc --noEmit && npm run build`
Then load `/kaam`. Expected: the approval queue renders full width, and the "Not contacted" section lists the four customers dropped from the win-back offer with "no marketing consent on file".

- [ ] **Step 5: Commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git add frontend/src
git -c commit.gpgsign=false commit -q -m "Show who was not contacted, and why

The compliance screen has always returned its refusals; nothing displayed them. A guardrail
nobody can see is decoration."
```

---

### Task 6: Memory page

**Files:**
- Modify: `frontend/src/components/MemoryGraph.tsx` (variant prop; raise the caps)
- Rewrite: `frontend/src/pages/MemoryPage.tsx`
- Modify: `frontend/src/styles/pages.css` (append)

**Interfaces:**
- Consumes: `useApp()` — `graph`, `memory`, `runMemorySearch`, `busy`.
- Produces: `<MemoryGraph variant="panel" | "page" />` (defaults `"panel"`).

- [ ] **Step 1: Give MemoryGraph a variant prop**

Replace the module-level constants with variant-aware values. The current constants are `MAX_BODIES = 26` and the label slice `labelled.slice(0, 6)`.

```tsx
export interface MemoryGraphProps {
  /** `page` has room for the graph's actual shape; `panel` is the rail's 150px box. */
  variant?: 'panel' | 'page'
}

/** Node caps per variant. The panel had to cull hard to stay legible; the page does not. */
const MAX_BODIES: Record<'panel' | 'page', number> = { panel: 26, page: 90 }
const MAX_LABELS: Record<'panel' | 'page', number> = { panel: 6, page: 20 }
```

Thread `variant` into `chooseBodies` (take the cap as a parameter rather than reading the constant) and into the label slice. The force simulation itself is unchanged.

- [ ] **Step 2: Write the page**

```tsx
import { MemoryGraph } from '../components/MemoryGraph'

/**
 * The knowledge graph at the size it deserves.
 *
 * In the rail this lived in a box about 330×150 pixels, which is why it had to be culled to 26
 * nodes to stay legible at all. Here it keeps its shape, and a recall lights the path it walked.
 */
export function MemoryPage(): JSX.Element {
  return (
    <main className="page">
      <header className="page__head">
        <h1 className="page__title deva" lang="hi">
          याददाश्त
        </h1>
        <p className="page__lede">
          What MunshiJi remembers about the shop, and how it found it. Ask something and the nodes
          it walked light up — vector similarity picks the seeds, graph traversal returns the
          relationships.
        </p>
      </header>
      <div className="page__body page__body--memory">
        <MemoryGraph variant="page" />
      </div>
    </main>
  )
}
```

- [ ] **Step 3: Append the CSS**

```css
/* ================================================================ memory page */

.page__body--memory {
  padding: 0;
  display: flex;
}

.page__body--memory .area-memory {
  flex: 1;
  border: none;
  box-shadow: none;
  border-radius: 0;
}

/* On the page the constellation takes the height it needs and the hits read beside it. */
.page__body--memory .memory__body {
  grid-template-rows: none;
  grid-template-columns: minmax(0, 1.6fr) minmax(280px, 1fr);
}

.page__body--memory .memory__canvas {
  border-bottom: none;
  border-right: 1px solid var(--rule);
}
```

- [ ] **Step 4: Verify**

Run: `npx tsc --noEmit && npm run build`
Then load `/yaaddasht`, type "winback offer" and search. Expected: the chip reads "N yaad aaye" in gold, the recalled nodes light in their kind colours with labels, everything else dims, and the hits read at full width beside the graph.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git add frontend/src
git -c commit.gpgsign=false commit -q -m "Give the knowledge graph room to be a graph

At 330x150 it had to be culled to 26 nodes to stay legible. On its own page it keeps 90 and
labels what a recall found."
```

---

### Task 7: Advice page

**Files:**
- Modify: `frontend/src/components/InsightFeed.tsx` (variant prop)
- Rewrite: `frontend/src/pages/AdvicePage.tsx`
- Modify: `frontend/src/styles/pages.css` (append)

**Interfaces:**
- Consumes: `useApp().insights` (`InsightListOut`). Each `InsightOut` has `title_hi`, `title_en`, `body_hi`, `body_en`, `metrics` (a `JsonObject`), `impact` (`Money`), `confidence`, `score`, `severity`, `suggested_tool`.
- Produces: `<InsightFeed variant="rail" | "page" />` (defaults `"rail"`).

The spec flags this page as the one at risk of feeling thin. The mitigation is to show the statistics behind each finding — `metrics` is returned on every insight and no view displays it.

- [ ] **Step 1: Give InsightFeed a variant prop**

```tsx
export interface InsightFeedProps {
  variant?: 'rail' | 'page'
}

export function InsightFeed({ variant = 'rail' }: InsightFeedProps): JSX.Element {
```

When `variant === 'page'`, drop the `.panel` chrome and the scroll container, and render each card with `.insight--page` added so the CSS can open it up.

- [ ] **Step 2: Render the metrics on the page variant**

Inside the insight card, when `variant === 'page'`, add beneath the body:

```tsx
{variant === 'page' && Object.keys(insight.metrics).length > 0 ? (
  <dl className="insight__metrics">
    {Object.entries(insight.metrics).map(([key, value]) => (
      <div key={key}>
        <dt>{key.replace(/_/g, ' ')}</dt>
        <dd className="tabular">{typeof value === 'number' ? value.toLocaleString('en-IN') : String(value)}</dd>
      </div>
    ))}
  </dl>
) : null}
```

- [ ] **Step 3: Write the page**

```tsx
import { InsightFeed } from '../components/InsightFeed'

/**
 * Every finding, with the arithmetic that produced it.
 *
 * The rail could only show a headline and a rupee figure. Here each finding carries the numbers
 * the engine actually worked from, which is the difference between advice and an assertion.
 */
export function AdvicePage(): JSX.Element {
  return (
    <main className="page">
      <header className="page__head">
        <h1 className="page__title deva" lang="hi">
          सलाह
        </h1>
        <p className="page__lede">
          Ranked by what it is worth and how sure the engine is. Each finding shows the figures it
          was computed from — nothing here is a guess dressed as a number.
        </p>
      </header>
      <div className="page__body">
        <InsightFeed variant="page" />
      </div>
    </main>
  )
}
```

- [ ] **Step 4: Append the CSS**

```css
/* ================================================================ advice page */

.insight--page {
  padding: var(--s-4) var(--s-5);
}

.insight--page .insight__title {
  font-size: var(--t-lg);
}

.insight--page .insight__body {
  max-width: 72ch;
}

.insight__metrics {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: var(--s-2) var(--s-4);
  margin: var(--s-3) 0 0;
  padding-top: var(--s-3);
  border-top: 1px solid var(--rule);
}

.insight__metrics div {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.insight__metrics dt {
  font-size: var(--t-label);
  letter-spacing: var(--track-label);
  text-transform: uppercase;
  color: var(--ink-3);
}

.insight__metrics dd {
  margin: 0;
  font-family: var(--font-mono);
  font-size: var(--t-sm);
  color: var(--ink);
  overflow-wrap: anywhere;
}
```

- [ ] **Step 5: Verify**

Run: `npx tsc --noEmit && npm run build`
Then load `/salah`. Expected: each finding is a full-width card whose metrics grid shows the underlying figures (baseline, z-score, counts). If a card still looks thin, that is the signal from the spec's risk section — the metrics grid is the fix and it is now in place.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/HP/OneDrive/Desktop/paytm
git add frontend/src
git -c commit.gpgsign=false commit -q -m "Show the arithmetic behind each finding

metrics has always been on the wire and no view displayed it. Showing it is the difference
between advice and an assertion."
```

---

### Task 8: Deep links on the host, and the deploy

**Files:**
- Modify: `vercel.json` (in the `paytm` repo staging directory)

**Interfaces:**
- Consumes: nothing.
- Produces: a working `/sehat` on refresh.

- [ ] **Step 1: Add the SPA rewrite**

The file currently holds `installCommand`, `buildCommand`, `outputDirectory`, `framework` and `build.env`. Add:

```json
"rewrites": [{ "source": "/((?!assets/).*)", "destination": "/index.html" }]
```

The negative lookahead keeps `/assets/*` serving real files; without it the rewrite would swallow the JS and CSS bundles and the page would render blank.

- [ ] **Step 2: Sync both push repositories from the working tree**

```bash
SCR=/c/Users/HP/AppData/Local/Temp/claude/C--Users-HP-OneDrive-Desktop-paytm/c3005b90-f294-4bfa-a8b2-37c2e9aa8ae4/scratchpad
SRC=/c/Users/HP/OneDrive/Desktop/paytm
rm -rf "$SCR/repo-paytm/frontend"
cp -r "$SRC/frontend" "$SCR/repo-paytm/frontend"
rm -rf "$SCR/repo-paytm/frontend/node_modules" "$SCR/repo-paytm/frontend/dist" "$SCR/repo-paytm/frontend/.env"
cp -r "$SRC/docs" "$SCR/repo-munshi/docs"
```

- [ ] **Step 3: Commit and push both**

```bash
cd "$SCR/repo-paytm" && git add -A && git -c commit.gpgsign=false commit -q -m "Split the companion screen into five pages

Everything that was squeezed now has its own room: the knowledge graph, the merchant-health
signal, the compliance refusals, and the ranked findings with their arithmetic.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
GIT_TERMINAL_PROMPT=0 timeout 90 git push origin main

cd "$SCR/repo-munshi" && git add -A && git -c commit.gpgsign=false commit -q -m "Add the multi-page design spec and plan

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
GIT_TERMINAL_PROMPT=0 timeout 90 git push origin main
```

- [ ] **Step 4: Verify the deploy**

Wait for Vercel, then:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://paytm-nu-seven.vercel.app/sehat
```

Expected: `200`. Then open `https://paytm-nu-seven.vercel.app/sehat` directly and refresh it — the health page must render, not a 404.

- [ ] **Step 5: Final browser pass**

Load all five routes at 1440×900 in **both** themes. Confirm: tabs navigate, the active tab joins the page, the call strip is present and usable on the four non-call pages, keyboard focus is visible on the tabs, and nothing overflows horizontally.

---

## Notes for the implementer

- **`variant` props default to the current behaviour** (`full`, `rail`, `panel`) so that if a page is built out of order, nothing that already works breaks.
- **Do not touch `tokens.css`.** If a new colour seems necessary, it isn't — use an existing token.
- **The store needs no changes.** `AppProvider` already loads the dashboard, merchant health, insights, actions and graph on mount; pages read and render.
- **`MemoryGraph`'s force simulation is delicate** — it seeds deterministically so the layout is identical at every demo. Change the node cap and the label count only; leave `step()` and `draw()`'s physics alone.
