# Blue-IQ Design System

One visual language across the product platform and the UAT console - two
independent apps that are meant to look like a single product. This document is
the reference for what they share and the rules that are not negotiable per-app.

---

## 1. Two apps, one look

The product platform and the UAT console are **independent repositories with no
shared package and no build-time coupling**. Each owns its own
`app/globals.css` and its own `components/ui.tsx`, `icons.tsx` and `charts.tsx`.
That is deliberate: either app can be cloned, built and deployed on its own.

The cost of that independence is that consistency is a **convention, not a
mechanism**. These are the things that must stay identical, because they are what
make two separate apps read as one product:

| Keep in step | Why |
|---|---|
| The token block at the top of `app/globals.css` | Palette, surfaces and motion - the whole look |
| The three font families in `app/layout.tsx` | Typography is half of what makes sites feel like one product |
| The appearance of the primitives in `components/ui.tsx` | Buttons, cards, badges and tables should be indistinguishable |
| The `--viz-*` palette and chart conventions | Charts in either app should be readable the same way |

**When you change any of those in one app, mirror it in the other.** Each file
carries a comment saying so. What does *not* need to match is anything genuinely
app-specific: the UAT console has panels the product site has no use for, and
vice versa - only the shared vocabulary needs to agree.

> Both apps previously drifted on exactly this: the console rendered
> `accent-700` as cobalt `#1b3fb0` instead of the Blue-IQ deep blue `#002181`,
> and used Bricolage Grotesque + Plus Jakarta Sans against the product site's
> Space Grotesk + Inter. They now match. If you are about to change a colour or a
> font in one app only, that is the drift starting again.

## 2. The visual language

**Enterprise, not editorial.** A cool-white canvas, deep navy ink, and the
Blue-IQ deep blue as the single primary. Solid fills only - no decorative
gradients.

| Token | Value | Role |
|---|---|---|
| `--paper` | `#f7f9fc` | Page canvas |
| `--surface` | `#ffffff` | Raised cards and sheets |
| `--ink` | `#0e1626` | Primary text |
| `--ink-soft` | `#4a5568` | Secondary text |
| `--line` / `--line-strong` | `#e6e9f1` / `#ced5e2` | Hairlines and borders |
| `--color-accent-700` | `#002181` | **The** Blue-IQ primary |
| `--color-accent-500` | `#2c49d6` | Brighter cobalt mid, focus rings |
| `--color-brass-400/500/600` | amber ramp | Sparing highlights: low confidence, flags |

**Typography** is three families, identical on both sites: **Space Grotesk**
(display), **Inter** (UI), **IBM Plex Mono** (keys, IDs, code, all numerics).
These are set per-app in `app/layout.tsx` and must not diverge - half of what
makes two sites read as one product is the type.

Dark mode is deliberately **disabled**: `@custom-variant dark` is rebound to a
class that is never applied, so OS dark mode cannot half-apply stray `dark:`
utilities left in either codebase.

---

## 3. Data visualisation

The chart palette is **computed and validated**, not chosen by eye. Categorical
hues are assigned in fixed order (`--viz-1` ... `--viz-6`) and are never cycled or
reassigned by rank, so filtering a series never repaints the survivors.

| Token | Value | |
|---|---|---|
| `--viz-1` | `#2c49d6` | brand cobalt |
| `--viz-2` | `#b07a08` | brass |
| `--viz-3` | `#0891b2` | cyan |
| `--viz-4` | `#b91c1c` | red |
| `--viz-5` | `#6d28d9` | violet |
| `--viz-6` | `#15803d` | green |

Validated against the `#ffffff` chart surface on the lightness band, chroma
floor, colour-vision-deficiency separation, normal-vision floor, and contrast -
all pass, worst adjacent pair delta E 19.1 (protan). **Re-run the validator before
changing any value.**

`--viz-good` / `--viz-warning` / `--viz-serious` / `--viz-critical` are reserved
for *state* and are never reused as "series 7". They always ship with a label or
icon, so state is never carried by colour alone.

### Non-negotiable chart rules

- **One axis.** Never two y-scales on one plot. Two measures of different scale
  become two charts.
- **A legend is always present for >=2 series**; a single series needs none
  because the title names it.
- **Every plot has a hover layer** - crosshair and tooltip on lines and areas,
  per-mark hover on bars.
- **Text wears ink tokens, never the series colour.** A coloured mark beside the
  label carries identity; the label itself stays in ink.
- **Grid and axes stay recessive**; 2px lines, >=8px hover markers, 4px rounded
  bar ends, 2px surface gaps between adjacent bars.

Available primitives: `StatCard`, `Sparkline`, `AreaChart`, `LineChart`,
`BarChart`, `Donut`, `BarList`, `Legend`.

---

## 4. Data tables

Tables are the densest thing on these screens, so the primitives carry the rules
instead of each page re-inventing them. Use `Table` / `THead` / `TBody` / `TR` /
`TH` / `TD` - never a raw `<table>`.

- The scroll wrapper is the **only** horizontally scrolling element, so the page
  body never scrolls sideways on mobile. Use `TableWrap` for a standalone table
  (it draws the frame) or `TableScroll` for a table already inside a `Card` (the
  card supplies the only border).
- `<TH numeric>` / `<TD numeric>` right-align and set `tabular-nums` - every
  numeric column must use them so digits line up.
- Rows are separated by hairlines, never zebra fills. The header is sticky.
- Empty and loading states are `EmptyState` and `Skeleton`, not ad-hoc text.

---

## 5. Icons

`components/icons.tsx` is a hand-drawn set, not an icon-font dependency: 24px
viewBox, 20px default render, `currentColor`, 1.7-1.8 stroke. Keeping one stroke
weight across the whole set is what makes them read as a family.

Domain marks: `KeyIcon`, `JobsIcon`, `TokenIcon`, `SuccessIcon`, `ClockIcon`,
`UsersIcon`, `ScanIcon`, `WebhookIcon`, `DocsIcon`, `ShieldIcon`, `UploadIcon`,
`ChartIcon`, `GaugeIcon`, `AlertIcon`, `DatabaseIcon`.
Interface marks: `RefreshIcon`, `SearchIcon`, `DownloadIcon`, `FilterIcon`,
`CopyIcon`, `ExternalIcon`, `ChevronIcon`, `MenuIcon`, `CloseIcon`, `PlayIcon`.

When you need a new icon, add it to `components/icons.tsx` - do not inline a
one-off SVG in a page. If the other app needs the same mark, copy it across so
the two sets stay recognisably the same family.

---

## 6. Motion

Motion is functional, never decorative. Entrances (`animate-fade-up`, `pop-in`),
the scroll-triggered `reveal` (toggled by the product site's `<Reveal>`
component) and its one-shot sibling `animate-reveal`, and `grow-x` for a
confidence bar that fills to the score it represents.

Every animation is disabled under `prefers-reduced-motion: reduce`. If you add
one, add it to that block too.
