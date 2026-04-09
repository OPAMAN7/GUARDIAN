```markdown
# Design System Specification: The Guardian’s Atelier

## 1. Overview & Creative North Star
The core philosophy of this design system is **"Serene Vigilance."** In the high-stakes world of parental digital safety, we move away from the "surveillance" aesthetic (harsh reds, rigid grids, and alert-heavy interfaces) toward a "curated sanctuary." 

Our Creative North Star is **The Digital Curator**. This system should feel like a high-end editorial publication—spacious, authoritative, and calming. We break the "template" look by utilizing intentional asymmetry, overlapping "glass" surfaces, and a typography scale that favors breathing room over density. We are not just building a dashboard; we are building a space where parents feel empowered and at peace.

---

## 2. Colors & Surface Logic
The palette is a sophisticated interplay of deep teals (`primary`) and ethereal, cool neutrals (`surface-container`). 

### The "No-Line" Rule
Traditional 1px borders are strictly prohibited for sectioning. They create visual noise and "box in" the user’s focus. Boundaries must be defined through **Background Color Shifts**. To separate a sidebar from a main feed, place a `surface-container-low` section against a `surface` background. Let the change in tone define the edge.

### Surface Hierarchy & Nesting
Treat the UI as a series of physical layers, similar to stacked sheets of heavy-weight cotton paper or frosted glass.
*   **Base:** `surface` (#f7f9fb)
*   **Sectioning:** `surface-container-low` (#f2f4f6)
*   **Active Elements/Cards:** `surface-container-lowest` (#ffffff) for maximum "lift."
*   **Emphasis:** `surface-container-high` (#e6e8ea) for nested utility panels.

### The "Glass & Gradient" Rule
To elevate the dashboard beyond a standard SaaS tool, use **Glassmorphism** for floating elements (like hover tooltips or mobile navigation docks). Use semi-transparent `surface` colors with a `backdrop-filter: blur(12px)`. 

### Signature Textures
For high-impact CTAs and Hero Data Cards, use a subtle **linear gradient** (135°) transitioning from `primary` (#004d64) to `primary-container` (#006684). This adds "soul" and depth that flat hex codes cannot provide.

---

## 3. Typography: The Editorial Voice
We utilize a dual-font strategy to balance character with utility.

*   **Display & Headlines (Manrope):** This is our "Editorial" voice. Use `display-lg` and `headline-md` for high-level summaries. The wider apertures of Manrope feel modern and approachable.
*   **Body & Titles (Inter):** This is our "Utility" voice. Inter is used for all data-rich areas, labels, and fine print to ensure maximum legibility at small sizes.

**The Hierarchy Rule:** Always pair a large `headline-lg` (Manrope) with a much smaller `label-md` (Inter) for metadata. This high-contrast scale communicates a premium, intentional design language.

---

## 4. Elevation & Depth
In this design system, shadows are a last resort, not a default.

*   **Tonal Layering:** Achieve 90% of your hierarchy by stacking `surface-container` tiers. A `surface-container-lowest` card on a `surface-container-low` background provides a soft, natural lift.
*   **Ambient Shadows:** If a card must "float" (e.g., a modal), use an ultra-diffused shadow: `box-shadow: 0 20px 40px rgba(25, 28, 30, 0.06);`. The shadow color is a tinted version of `on-surface`, never a neutral grey.
*   **The Ghost Border Fallback:** For accessibility in form fields, use a "Ghost Border": `outline-variant` (#bfc8cd) at **20% opacity**. It provides a hint of structure without interrupting the visual flow.

---

## 5. Components

### Buttons & Chips
*   **Primary Button:** Uses the signature gradient (Primary to Primary-Container). Roundedness: `full`.
*   **Secondary Button:** `surface-container-highest` background with `on-surface` text. No border.
*   **Chips:** Use `secondary-container` for active states. Chips should be pill-shaped (`rounded-full`) to contrast against the softer `md` corners of the cards.

### Data Cards & Lists
*   **The No-Divider Rule:** Never use horizontal lines to separate list items. Use vertical white space (16px–24px) or a alternating subtle background shift.
*   **Asymmetric Cards:** For "Priority Alerts," use a 4px left-accent bar in `error` (#ba1a1a) or `tertiary` (#005049) rather than a full border.

### Status Indicators
*   **Safe/Active:** `tertiary` (#005049) with a soft glow effect (`drop-shadow` using the tertiary color at 30% opacity).
*   **Alert:** `error` (#ba1a1a) used sparingly. For high-end appeal, use the `error-container` (#ffdad6) as the background for the alert card to soften the blow.

### Form Elements
*   **Inputs:** Utilize `surface-container-low`. Upon focus, transition the background to `surface-container-lowest` and apply a 1px "Ghost Border."
*   **Checkboxes/Radios:** Use `primary` (#004d64) for checked states. The inner icon or dot should always be `on-primary` (#ffffff).

### Bespoke Component: The "Time-Aisle"
For parental controls, we replace the standard bar chart with a "Time-Aisle"—a horizontal, soft-edged track using `secondary-container` where "active time" blocks are layered as `primary` glass-morphic pills.

---

## 6. Do’s and Don’ts

### Do:
*   **Do** use `rounded-xl` (1.5rem) for large dashboard containers to create a soft, friendly environment.
*   **Do** utilize `tertiary` colors for "Positive Reinforcement" features (e.g., educational goals reached).
*   **Do** embrace white space. If a layout feels "empty," increase the font size of the headlines rather than adding more borders or boxes.

### Don't:
*   **Don't** use 100% black (#000000). Always use `on-surface` (#191c1e) for text to maintain a premium, ink-on-paper feel.
*   **Don't** use "Alert Red" for anything other than critical security threats. Use `secondary` or `outline` for neutral states.
*   **Don't** crowd the edges. Maintain a minimum 32px inner padding on all cards. Space is a luxury; use it.

---
**Director's Note:** This system succeeds when it feels "invisible." The parent should feel the security of the software through the calm of the interface. If a screen feels "busy," remove a border and add a background tone shift. Keep it breathable, keep it editorial.```