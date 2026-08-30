---
name: Steward Narrative
colors:
  surface: '#f8f9ff'
  surface-dim: '#ccdbf4'
  surface-bright: '#f8f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#eff4ff'
  surface-container: '#e6eeff'
  surface-container-high: '#dde9ff'
  surface-container-highest: '#d5e3fd'
  on-surface: '#0d1c2f'
  on-surface-variant: '#3e4947'
  inverse-surface: '#233144'
  inverse-on-surface: '#ebf1ff'
  outline: '#6e7977'
  outline-variant: '#bdc9c6'
  surface-tint: '#006a63'
  primary: '#005c55'
  on-primary: '#ffffff'
  primary-container: '#0f766e'
  on-primary-container: '#a3faef'
  inverse-primary: '#80d5cb'
  secondary: '#545f73'
  on-secondary: '#ffffff'
  secondary-container: '#d5e0f8'
  on-secondary-container: '#586377'
  tertiary: '#4d5255'
  on-tertiary: '#ffffff'
  tertiary-container: '#656a6d'
  on-tertiary-container: '#e6eaee'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#9cf2e8'
  primary-fixed-dim: '#80d5cb'
  on-primary-fixed: '#00201d'
  on-primary-fixed-variant: '#00504a'
  secondary-fixed: '#d8e3fb'
  secondary-fixed-dim: '#bcc7de'
  on-secondary-fixed: '#111c2d'
  on-secondary-fixed-variant: '#3c475a'
  tertiary-fixed: '#dfe3e7'
  tertiary-fixed-dim: '#c3c7cb'
  on-tertiary-fixed: '#171c1f'
  on-tertiary-fixed-variant: '#43474b'
  background: '#f8f9ff'
  on-background: '#0d1c2f'
  surface-variant: '#d5e3fd'
typography:
  headline-lg:
    fontFamily: Geist
    fontSize: 30px
    fontWeight: '600'
    lineHeight: 38px
    letterSpacing: -0.02em
  headline-lg-mobile:
    fontFamily: Geist
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Geist
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
  body-lg:
    fontFamily: Geist
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Geist
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-md:
    fontFamily: Geist
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.02em
  label-caps:
    fontFamily: Geist
    fontSize: 11px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.05em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  base: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 24px
  xl: 32px
  container-max: 1024px
  gutter: 20px
---

## Brand & Style

This design system is built for clarity, precision, and the quiet confidence required in financial advisory. The brand personality is that of a "Knowledge Steward": authoritative yet accessible, calm, and strictly fact-based. 

The visual style leans into **Modern Minimalism** with a focus on high-quality typography and generous whitespace to reduce cognitive load. It avoids the aggressive "growth-oriented" aesthetics of typical fintech apps (bright greens, loud gradients) in favor of a stable, institutional atmosphere that prioritizes the delivery of complex information over marketing flair.

The UI should evoke a sense of organized intelligence through systematic alignment, a restrained color palette, and subtle tactile cues that make the interface feel reliable and grounded.

## Colors

The palette is anchored by **Deep Teal (#0F766E)**, chosen for its association with stability and sophisticated growth without the volatility of brighter greens. **Muted Navy (#1E293B)** provides a strong foundation for high-contrast text and primary navigation elements.

- **Backgrounds:** Use `#F8FAFC` for the main application canvas to differentiate from card surfaces.
- **Surfaces:** Use pure white `#FFFFFF` for primary content containers and cards to create a clear "layering" effect.
- **Text:** Maintain a strict hierarchy using `#1E293B` for headers and primary body text, and `#64748B` for secondary meta-data or supportive labels.
- **Accents:** Avoid using the primary teal for destructive or purely decorative actions; reserve it for progress indicators and primary CTA states.

## Typography

The design system utilizes **Geist** for its technical precision and exceptional legibility in data-heavy environments. The monospaced-influenced proportions lend an air of "engineered trust."

- **Scale:** High contrast between headlines and body text is avoided to keep the interface feeling calm and unified.
- **Weight:** Use Semi-Bold (600) sparingly for headlines. Body text should remain at Regular (400) to ensure the interface doesn't feel overly dense.
- **Readability:** Line heights are set slightly wider (1.5x for body) to assist in reading long-form financial explanations and FAQ answers.

## Layout & Spacing

This design system uses a **Fixed-Fluid Hybrid** layout. On desktop, content is centered within a 1024px container to prevent line lengths from becoming illegible. 

- **Grid:** A 12-column grid is used for dashboard views, while a single-column "Feed" layout is preferred for the FAQ assistant interface to maintain focus.
- **Spacing Rhythm:** An 8pt linear scale is the primary driver, but a 4pt "half-step" is permitted for tight component internals (e.g., checkbox labels, small icons).
- **Responsive:** Breakpoints occur at 640px (Mobile) and 1024px (Tablet/Desktop). On mobile, horizontal margins shrink to 16px to maximize real estate for data tables.

## Elevation & Depth

To maintain a professional and trustworthy feel, this design system avoids heavy shadows. Instead, it utilizes **Tonal Layers** supplemented by very soft, highly diffused ambient shadows.

- **Level 0 (Background):** `#F8FAFC` - The base of the application.
- **Level 1 (Cards/Surface):** `#FFFFFF` with a 1px border of `#E2E8F0`. This is the standard for FAQ items and data modules.
- **Level 2 (Active/Floating):** Use a soft shadow: `0px 4px 12px rgba(30, 41, 59, 0.05)`. This is reserved for hover states and dropdown menus.
- **Interaction:** Depth should be subtle. When a user interacts with a card, the shadow should slightly expand rather than the card changing color, maintaining a tactile, paper-like feel.

## Shapes

The shape language is defined by **Rounded (0.5rem / 8px)** corners for standard UI components like inputs and buttons. 

- **Cards:** Larger containers and cards use `rounded-lg` (16px) to appear more approachable and modern.
- **Pills:** Status indicators and category tags use a full pill radius to distinguish them from actionable buttons.
- **Consistency:** All interactive elements must maintain a corner radius; sharp edges are strictly prohibited as they evoke a level of "harshness" that conflicts with the "calm" brand goal.

## Components

### Buttons
- **Primary:** Deep Teal background with white text. Solid, no gradient.
- **Secondary:** Muted Navy outline (1px) with navy text.
- **Ghost:** Navy text with no background; background appears as `#F1F5F9` on hover.

### Cards
- White background, 16px corner radius, 1px `#E2E8F0` border.
- Use internal padding of 24px (lg) for general content and 16px (md) for densified data.

### Info Pills
- Neutral styling only. Use `#F1F5F9` background with `#475569` text.
- Avoid "Traffic Light" colors (Red/Yellow/Green) unless indicating absolute financial risk.

### Input Fields
- Background: `#FFFFFF`. Border: 1px `#CBD5E1`. 
- Focused state: 1px Deep Teal border with a soft teal outer glow (3px).
- Labels are always positioned above the field in `label-caps` style for clarity.

### FAQ List Items
- Collapsible accordions with a clean divider line (`#F1F5F9`).
- Use a chevron-down icon in Muted Navy to indicate expandability.

### Data Tables
- Header row uses `#F8FAFC` background with `label-caps` text.
- Row dividers are 1px, `#F1F5F9`. No vertical borders between columns.