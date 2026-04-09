# Stitch Handoff Template

Fill this file and send it back to start dashboard overhaul implementation.

## 1) Product Identity
- Product name:
- Tagline:
- Tone (strict security / family-safe / enterprise):

## 2) Navigation Map
List pages in nav order.

Example format:
1. Live View
2. Devices
3. Blocklist
4. Alerts
5. History
6. Settings

Your nav map:
1. Live View
2. Devices
3. Blocklist
4. Rules and Limits
5. Alerts
6. Settings (planned)

## 3) Page Layout Specs
For each page, define:
- desktop layout blocks
- mobile layout behavior
- key components

### Page: Live View
- Desktop: left navigation, top status bar, live screen panel, connected devices list, stream controls card, output controls card, quick stats cards
- Mobile: stacked panels, live screen first, controls collapsed below feed
- Components: device selector, live badge, stream sliders (fps/quality), width selector, apply button, output controller launcher

### Page: Devices
- Desktop: full-width table with status, device metadata, remove action
- Mobile: card list with condensed status and actions
- Components: device row/card, status dot, delete action with confirmation

### Page: Blocklist
- Desktop: add-domain input row + blocked domain table
- Mobile: add-domain at top + stacked domain cards
- Components: domain input, add button, category badge, unblock action

### Page: Alerts
- Desktop: alert feed with severity icon, message, device, timestamp, mark-all-read action
- Mobile: stacked alert cards with severity highlight
- Components: alert card, unread indicator, mark-all-read control

### Page: History
- Desktop: planned timeline view for app/site events
- Mobile: planned compact chronological cards
- Components: date filter, device filter, timeline/event rows

### Page: Settings
- Desktop: planned security and retention controls
- Mobile: planned simplified toggle-based controls
- Components: policy toggles, retention selectors, branding panel

## 4) Component Library
List components and states.

Example:
- DeviceCard: online/offline/warning
- AlertItem: low/medium/high/critical
- StatusBadge: connected/disconnected

Your list:
- DeviceCard: online/offline/warning
- AlertItem: low/medium/high/critical
- StatusBadge: connected/disconnected
- DomainRow: blocked/unblocked states
- StreamControl: slider/select/button group
- ModalConfirm: destructive confirmation

## 5) Design Tokens

## Color tokens
- primary:
- accent:
- danger:
- warning:
- success:
- background:
- surface:
- text:
- text-muted:

## Typography
- Headline font:
- Body font:
- Monospace font:

## Spacing scale
- xs:
- sm:
- md:
- lg:
- xl:

## Radius and shadow
- radius-sm:
- radius-md:
- radius-lg:
- shadow-card:
- shadow-overlay:

## 6) Interaction Rules
- Toast style and duration:
- Modal behavior:
- Confirmation prompts:
- Empty states:
- Loading states:
- Error states:
- Animation speed/style:

## 7) Security Visual Rules
- How critical alerts must look:
- Connection-status indicator style:
- Trust cues (badges, banners, audit chips):

## 8) Priority Build Order
Rank pages by build priority.

1.
2.
3.
4.
5.

## 9) Non-Negotiables
List hard requirements that must not be changed.

-
-
-
