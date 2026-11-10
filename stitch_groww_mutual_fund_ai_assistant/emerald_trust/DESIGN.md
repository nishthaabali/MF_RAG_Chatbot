---
name: Emerald Trust
colors:
  surface: '#f9f9ff'
  surface-dim: '#d6dae6'
  surface-bright: '#f9f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f0f3ff'
  surface-container: '#eaeefa'
  surface-container-high: '#e4e8f5'
  surface-container-highest: '#dee2ef'
  on-surface: '#171c25'
  on-surface-variant: '#3c4a43'
  inverse-surface: '#2c313a'
  inverse-on-surface: '#ecf0fd'
  outline: '#6b7b72'
  outline-variant: '#bacac1'
  surface-tint: '#006c4f'
  primary: '#006c4f'
  on-primary: '#ffffff'
  primary-container: '#00d09c'
  on-primary-container: '#00533c'
  inverse-primary: '#2fe0aa'
  secondary: '#3247e2'
  on-secondary: '#ffffff'
  secondary-container: '#4f63fb'
  on-secondary-container: '#fffbff'
  tertiary: '#934b07'
  on-tertiary: '#ffffff'
  tertiary-container: '#ffa15b'
  on-tertiary-container: '#733800'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#59fdc5'
  primary-fixed-dim: '#2fe0aa'
  on-primary-fixed: '#002116'
  on-primary-fixed-variant: '#00513b'
  secondary-fixed: '#dfe0ff'
  secondary-fixed-dim: '#bcc2ff'
  on-secondary-fixed: '#000b62'
  on-secondary-fixed-variant: '#102bcd'
  tertiary-fixed: '#ffdcc6'
  tertiary-fixed-dim: '#ffb785'
  on-tertiary-fixed: '#301400'
  on-tertiary-fixed-variant: '#713700'
  background: '#f9f9ff'
  on-background: '#171c25'
  surface-variant: '#dee2ef'
typography:
  display-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 48px
    fontWeight: '700'
    lineHeight: 56px
    letterSpacing: -0.02em
  headline-xl:
    fontFamily: Plus Jakarta Sans
    fontSize: 36px
    fontWeight: '700'
    lineHeight: 44px
    letterSpacing: -0.02em
  headline-xl-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 28px
    fontWeight: '700'
    lineHeight: 36px
    letterSpacing: -0.01em
  headline-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: 0em
  headline-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 16px
    fontWeight: '600'
    lineHeight: 24px
    letterSpacing: 0em
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
    letterSpacing: 0em
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
    letterSpacing: 0em
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
    letterSpacing: 0em
  label-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '600'
    lineHeight: 20px
    letterSpacing: 0.01em
  label-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.02em
  num-metric-lg:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '600'
    lineHeight: 36px
    letterSpacing: -0.02em
  num-metric-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '500'
    lineHeight: 24px
    letterSpacing: -0.01em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1.5rem
  gutter-mobile: 1rem
  margin: 2rem
  margin-mobile: 1rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 1rem
  space-lg: 1.5rem
  space-xl: 2rem
---

## Brand & Style
The design system establishes a transparent, frictionless, and disciplined visual environment designed for modern wealth building and personal finance. It balances retail accessibility with institutional-grade credibility, stripping away superfluous ornament to emphasize legibility, precision, and confidence.

The design style sits at the intersection of modern corporate clarity and purposeful minimalism. Interfaces prioritize whitespace, crisp geometry, and structural order. Micro-interactions are deliberate and tactile, while data structures remain scannable and uninhibited by visual noise.

## Colors
The palette balances an assertive energetic primary tone with calming institutional anchors.

- **Primary Emerald (`#00D09C`):** Drives affirmative states, primary executions, growth signals, and key conversion pathways.
- **Secondary Trust Blue (`#5367FF`):** Reserved for verification badges, informational links, research citations, and institutional highlights.
- **Neutral Heading Slate (`#1E232C`):** High-contrast neutral for headings and critical metrics.
- **Neutral Body Muted (`#44475B`):** Calibrated for extended reading comfort across descriptions and tables.
- **Neutral Caption Subdued (`#7C7E8C`):** Low-emphasis metadata, timestamps, and secondary labels.
- **Surface Hierarchy:** Canvas rests on Pure White (`#FFFFFF`), with utility containers/sidebars using Light Cool Gray (`#F8F9FA`), and resting cards using Subtle Card Surface (`#F4F6F8`) framed by delicate structural borders (`#E9ECEF` / `#EAECEF`).

## Typography
Plus Jakarta Sans conveys confident, modern authority in headings, while Inter handles prolonged analytical reading with neutral clarity.

All numerical financial entities—including NAV, CAGR, AUM, Absolute Returns, and Expense Ratios—must enable OpenType tabular figures (`font-variant-numeric: tabular-nums;`) to align vertical decimals accurately. Primary metrics leverage negative tracking (`-0.01em` to `-0.02em`) to maintain tightness in dense portfolio dashboards.

## Layout & Spacing
The layout adheres to an 8pt architectural rhythm, translating into a structured 12-column responsive fluid grid on desktop and tablet, consolidating to a single/dual-column flow on mobile viewport sizes.

- **Desktop (≥ 1024px):** 12-column structure with `1.5rem` (24px) gutters and max-width clamping at 1280px or 1440px with `2rem` (32px) outer page margins.
- **Tablet (768px – 1023px):** 8-column layout, maintaining `1.5rem` gutters and `1.5rem` horizontal margins.
- **Mobile (≤ 767px):** 4-column layout transitioning to full-width cards; gutters collapse to `1rem` (16px) with `1rem` (16px) screen margins.
- **Component Flow:** Use `space-xs` (4px) and `space-sm` (8px) for tightly coupled financial indicators and their parent labels; utilize `space-md` (16px) for standard panel padding and form input gaps.

## Elevation & Depth
Depth is articulated primarily through surface value separation and micro-borders rather than aggressive drop shadows.

- **Resting Cards:** Elevated using subtle 1px border lines in `#EAECEF` over `#FFFFFF` or `#F4F6F8`, layered on top of an ambient soft shadow: `0 4px 12px rgba(0, 0, 0, 0.04)`.
- **Dropdowns & Popovers:** `0 8px 24px rgba(0, 0, 0, 0.08)` paired with a 1px `#E9ECEF` boundary line.
- **Interactive Hover:** Cards and interactive tiles scale subtly or step up with an augmented shadow: `0 8px 20px rgba(0, 0, 0, 0.06)`, keeping the physical metaphor quiet and precise.

## Shapes
Geometry uses clean, calibrated corners to retain an approachable yet structured profile.

- **Containers & Cards:** Strictly apply `12px` (`0.75rem`) border radii to create balanced containment.
- **Interactive Controls (Buttons, Inputs, Selectors):** Strictly apply `8px` (`0.5rem`) border radii for ergonomic tactile feedback.
- **Status Pills, Citations, & Tags:** Fully curved pill treatment (`9999px`) for contextual badges and verified state tags.

## Components

### Buttons
- **Primary:** Background in `#00D09C`, text in `#FFFFFF`, font weight `600`, border radius `8px`. Interactive states shift brightness downward by 6% on hover.
- **Secondary:** Background in `#FFFFFF`, border `1px solid #EAECEF`, text in `#1E232C`.
- **Ghost/Tertiary:** Background transparent, text in `#5367FF` or `#1E232C` with background fill transitioning to `#F8F9FA` on hover.

### Inputs & Form Fields
- Height `44px`, corner radius `8px`, border `1px solid #E9ECEF`, background `#FFFFFF`.
- Placeholder text in `#7C7E8C`.
- Focus ring: crisp `0 0 0 2px rgba(0, 208, 156, 0.2)` accompanied by border shift to `#00D09C`.

### Cards & Financial Panels
- Radius `12px`, background `#FFFFFF` or `#F4F6F8`, outer border `1px solid #EAECEF`, elevation `0 4px 12px rgba(0, 0, 0, 0.04)`.
- Internal metric blocks organize primary numbers above `#7C7E8C` micro-labels.

### Verification Pills & Status Badges
- **Verified Sources / Citations:** Pill-shaped (`9999px`), light tint background (`rgba(83, 103, 255, 0.08)`), text `#5367FF`, font weight `500`, font size `12px`.
- **Market Positives:** Tint `rgba(0, 208, 156, 0.12)`, text `#00A87E`.

### Lists & Tables
- Border bottom `1px solid #F4F6F8` per item. Hover rows trigger background transition to `#F8F9FA`.
- Columns holding NAV, returns, or monetary units must align right with fixed tabular alignment.

### Selection Controls (Checkboxes & Radios)
- Border `1.5px solid #E9ECEF`, radius `4px` for checkboxes and circular for radios. Checked state shifts background to `#00D09C` with white checkmark/dot.