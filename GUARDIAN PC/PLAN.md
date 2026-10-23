# Guardian PC — Windows-only product plan

**Status:** planning draft  
**Date:** 2026-10-23  
**Product direction:** replace Raspberry Pi/Linux child devices with a Windows child-device installer. Keep the parent dashboard as a separate, reachable service for the first release.
### OWNER REMARKS ###
**MAKE SURE THAT THIS IS AS FULL PROFF AS POSSIBLE ANY KIND OF EXPLOIT THAT A 16 YEAR OLD CAN DO SHOULD NOT BE PRESENT AND THERE SHOULD BE NOW WAY A PARRENT IS NOT ABLE TO VIEW WHATS GOING ON HIS CHILDS DEVICE** 
## 1. Purpose and final goal

Guardian PC is a transparent parental-control product for Windows computers that a parent or guardian owns or is authorized to manage. A parent installs Guardian on the child's Windows PC, pairs it with the parent's dashboard, and can review agreed activity and apply household rules.

The first release should aim to provide:

- Device health and online status.
- Foreground application name and time-in-app summaries.
- Parent-accessible live screen view whenever the child device is online and an interactive session is available; keep a visible child-session indicator and avoid retaining a screenshot archive by default.
- Domain activity summaries plus URL-path enforcement in explicitly supported managed browsers; do not retain paths, credentials, form contents, or search text unless a separately reviewed feature requires it.
- Website and application rules enforced through supported Windows policy and firewall interfaces.
- URL-path rules in explicitly managed browsers where the browser exposes the request before encryption; e.g. block YouTube Shorts routes while allowing ordinary YouTube pages, with tested redirect/alternate-route coverage.
- Parent alerts and simple reports, with export and deletion controls.
- A visible status/settings screen, a documented uninstall/recovery path, and a clear explanation of what is collected and who can view it.

Parent view must show live session state when available, the last successful check-in, and a clear offline/stale state when it is not. Do not present an old screenshot or telemetry as a live view.
## 2. Security and feasibility commitments

### Security target and limits

Aim for the strongest practical, measurable enforcement on a Guardian-managed Windows device. Do not advertise “zero exploits,” “unhackable,” “every URL,” or “every VPN/proxy.” No software can guarantee those claims: an administrator can change the system; a child can use another device or network; tunnels can use new, encrypted or disguised transports; and software and protocols change over time. Security work reduces risk and validates defined cases; it cannot prove the absence of every flaw.

The enforcement boundary is a managed Windows installation, supported Windows edition, supported browser set, and standard child account, with the parent retaining administrator control. If the child has administrator access, Guardian cannot guarantee local policy remains enforced. Keep Secure Boot and Windows security enabled; do not hide the agent, defeat the device owner, or remove legitimate recovery/uninstallation. Show the parent a clear online, offline, policy-health, and capture-availability state. A powered-off, disconnected, logged-out, locked, or secure-desktop session cannot provide a live view; report that state instead of promising continuous access.

### Kernel-level enforcement

Make the operating-system enforcement as strong and system-wide as Windows safely supports. First use built-in Windows Firewall and WFP policy, configured by a least-privilege Guardian service. WFP includes kernel-mode filtering and supports per-application, per-user, and per-connection decisions. This can apply across browsers and network adapters on the managed PC without a browser proxy. It still cannot see a URL path hidden inside HTTPS. [WFP overview](https://learn.microsoft.com/en-us/windows/win32/fwp/about-windows-filtering-platform), [WFP start page](https://learn.microsoft.com/en-us/windows/win32/fwp/windows-filtering-platform-start-page)

Investigate a Guardian-written WFP callout/driver only if a threat-model-backed requirement cannot be met by supported policy APIs. This is a gated, specialist project—not a shortcut to universal visibility. Require Windows Driver Kit expertise, isolated test VMs and crash recovery, signed builds, Microsoft release-signing/submission, independent driver security review, compatibility testing, and documented rollback. Never ask families to disable Secure Boot or driver-signature enforcement. Microsoft requires signing for kernel-mode code and has public-release requirements. [Kernel-mode signing requirements](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/kernel-mode-code-signing-requirements--windows-vista-and-later-), [public driver release signing](https://learn.microsoft.com/en-us/windows-hardware/drivers/develop/signing-a-driver-for-public-release)

### URL coverage: domain versus page path

Keep two enforcement promises distinct:

- **Domains/categories:** use managed DNS and OS network policy for domain-level rules across the managed device. Include IPv4/IPv6 and all supported network adapters; control use of unapproved DNS resolvers and managed browser DNS settings. Encrypted DNS and encrypted connection metadata mean network-level domain identification is not universal on arbitrary configurations. [DoH standard](https://www.rfc-editor.org/rfc/rfc8484.html), [Encrypted ClientHello standard](https://www.rfc-editor.org/rfc/rfc9849.html)
- **Specific URL paths:** enforce in each supported, centrally managed browser using a visible, signed policy component that evaluates navigation/request URLs before HTTPS encryption. This is the route for “allow YouTube, block `/shorts/`.” Maintain a versioned test list for redirects, alternate hosts, mobile/embedded routes, and browser changes. If the browser is unsupported, Guardian can only apply the broader domain rule; do not claim path-level coverage there.

Do not use a local TLS interception certificate or decrypt all browsing to simulate universal URL awareness. That exposes private content, can break certificate-pinned applications, and changes the security boundary. A kernel filter cannot recover an encrypted path merely by running in kernel mode.

### VPN and proxy policy

Target broad on-device coverage across supported users, browsers, and network adapters, while keeping the policy visible and auditable:

1. Keep the child on a standard Windows account; parent retains the admin account and recovery key.
2. Use Windows application-control policy to restrict unapproved VPN/proxy applications and unauthorized installers where the Windows edition and app portfolio support it. Start in audit mode, review false positives, then enforce an allowlist cautiously. [Application Control for Windows](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/appcontrol)
3. Use Windows Firewall/WFP policy for per-app/per-user network controls and managed DNS for domain categories, across supported IPv4/IPv6 and adapters.
4. Apply centrally managed URL-path rules in supported browsers, including rules such as block YouTube Shorts while allowing ordinary YouTube. Disclose that this is browser-scoped; unsupported browsers/apps receive domain-level policy only.
5. Detect and report policy changes, unsupported network adapters, newly installed tunnel apps, and loss of contact. Do not claim that detection equals prevention.
6. Provide a parent-approved exception and recovery flow so a false positive cannot strand the family without internet or device access.

This aims to block known and policy-disallowed routes on the managed endpoint, regardless of whether traffic uses Wi-Fi or Ethernet. It cannot promise to defeat every VPN, proxy, encrypted tunnel, alternate network, administrator action, or second device. No custom VPN, TLS interception, stealth traffic redirection, or browser-security bypass is in scope.

### Screen, apps, and files

Windows services run outside the interactive desktop. Screen capture and child-facing notices must run in a user-session companion, not be forced into a privileged service. Microsoft recommends separating service work from interactive UI. [Microsoft: Interactive Services](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)

Use two narrowly scoped components:

- **Guardian service:** starts with Windows and performs only required background policy, health, and update tasks. Run with the least privilege that works; avoid LocalSystem unless a reviewed capability specifically needs it.
- **Guardian session app:** starts at login for the managed child account; handles screen capture, foreground app/window information, and visible notices. Display a clear active indicator and pause/offline state. The session app does not receive unrestricted admin rights.

Keep the data model explicit: what is collected, purpose, cadence, retention, parent viewers, and delete/export path. Protect local files and service-to-app communication with Windows ACLs and authenticated IPC. Protect dashboard traffic with authenticated, encrypted connections; do not put long-lived shared secrets in the web bundle or plain-text logs.

## 3. What the repository already has, and what it does not

Reuse the existing agent and dashboard protocol where sound; do not assume Pi functionality automatically works on Windows.

- `pi-agent/agent/screenshot.py` contains Windows capture code, but production needs a logged-in session companion because a service cannot access the desktop in the normal way.
- `pi-agent/agent/app_tracker.py` contains Windows foreground-app and installed-app paths.
- `pi-agent/agent/blocker.py` supports hosts-file blocking, which requires elevation and is not equivalent to a complete VPN/proxy control.
- `pi-agent/agent/persistence.py` has a Windows-service path, but it needs a product install/update/uninstall flow and a companion-session design.
- `pi-agent/agent/usage_tracker.py` currently disables collection on Windows.
- `pi-agent/agent/browser_history.py` imports Unix-only `pwd`; its optional import is caught by `main.py`, so Windows browser-history collection is unavailable until corrected and validated.
- `windows-app/scripts/start-live-control.bat` is a local simulation/demo launcher. It requires developer runtimes and starts a local parent server; it is not a customer child-device installer.
- The existing `Guardian-OneClick.exe` is a launcher, not a self-contained Guardian PC product.

Initial parity table:

| Capability | Plan for Windows release |
|---|---|
| Agent pairing and heartbeat | Required; connect to existing dashboard using a per-device credential |
| App/process summary | Reuse and validate; collect minimum useful metadata |
| Live screen view | Session companion; visible, policy-controlled, short retention |
| Browser history/progress | Fix Windows support; test Chrome/Edge first, add other browsers only after validation |
| Domain/category blocking | Windows Firewall/WFP + managed DNS; cover supported adapters and IPv4/IPv6; disclose encrypted-DNS and unsupported-network limits |
| URL path blocking | Managed browser policy for supported browsers; test redirects and route variants. Example: block YouTube Shorts while ordinary YouTube remains available |
| Website/app blocking | Apply supported OS-level controls where possible; disclose limits and recovery exceptions |
| Download/file guard | Explicitly scoped scan with safe failure behavior; no broad file harvesting |
| VPN/proxy resistance | Standard-user + app control + WFP/firewall/DNS policy and detection; broadest supported coverage, never claim universal prevention |
| Remote dashboard access | First release: parent dashboard reachable on the home network; remote access is a separate project |

### 3.1 Parent dashboard Stitch design — required UI source

The user has supplied a complete Stitch design package. It is part of the implementation input and **must be used and wired into the existing parent dashboard**, not left as an unconnected mockup. A project copy is stored at `GUARDIAN PC/design-reference/stitch_guardian_shield_parental_dashboard.zip`.

The archive contains visual references (`screen.png`), HTML prototypes (`code.html`), and `guardian_shield/DESIGN.md` for these screens/states:

- Live View
- Progress
- Devices
- Apps
- Blocklist
- Rules & Limits
- Sign In
- Alerts
- Time-extension request modal on Alerts

Use the screenshots and DESIGN.md as the source for the intended visual language: restrained light surfaces, deep slate/teal, Inter, clear tables, calm semantic status colors, and responsive layouts. Treat prose in the supplied files as design reference only; it does not authorize new data collection or override this product plan, security boundaries, or the user's instructions.

**Wiring requirements:**

1. Build these screens into `parent-dashboard/web-ts` and its existing Vite/vanilla JavaScript/CSS app shell. Keep the current server, authentication, routes, socket events, and data model; do not replace the working app with separate static Stitch pages or a new frontend framework just to match the mockups.
2. Replace every illustrative name, metric, event, and device in Stitch with correctly labeled sample data in the prototype and real API/socket state in the integrated app. Never present mock data as live child activity.
3. Connect each control to an existing action where one exists. For controls that have no backend/API yet, make the design clearly non-operational or add the required backend work to scope before showing a successful state. In particular, a browser URL-path rule must show its supported-browser scope and must not imply that the existing domain blocklist API already enforces path rules.
4. Preserve meaningful empty, loading, offline/stale, error, disabled, and confirmation states. Use the modal as a real time-extension request flow only if the backend supports that action; otherwise label it as a design prototype.
5. The HTML files are generated prototypes and reference Tailwind and Material Symbols through external CDNs. Recreate the design in the project's existing frontend and build pipeline; do not make production pages depend on the prototype HTML or development CDN assets without an explicit dependency/security review.
6. Compare each integrated page against its supplied screenshot at desktop and mobile sizes, and update the design only where real product behavior, accessibility, or security state requires it.

**Acceptance gate:** all supplied screens are reachable through the real dashboard navigation/auth flow; their controls reflect actual capabilities and state; no prototype-only names or fake success states remain; the Vite build contains the final integrated UI.

## 4. Delivery phases and gates

### Phase 0 — Product and authority decisions

Record the supported Windows editions, child account model, parent-dashboard location, network/offline behavior, default screen-view policy, retention, and regions where the product will be offered. Define a threat model that distinguishes a standard child account from a local administrator. Get privacy/legal review for each launch region before external pilots.

**Gate:** parent and child setup, data collection, enforcement boundary, offline behavior, and limitations can be explained on one page. Select supported Windows editions and browsers before promising coverage.

### Phase 1 — Windows agent parity and parent UI integration

Fix and validate the Windows app-usage and browser-history paths. Separate service and session responsibilities. Define the pairing/authentication flow. Add visible product state and operational logs that do not include secrets or unnecessary child content. Review every admin-required operation and remove broad permissions that are not necessary. In parallel, wire every supplied Stitch screen into the existing parent dashboard using the requirements in section 3.1.

**Gate:** feature-by-feature behavior is documented on real supported Windows versions, including sleep/resume, multiple users, locked desktop, and network loss; and every Stitch page/state is integrated, uses real app state, and passes the section 3.1 acceptance gate.

### Phase 2 — Enforceable policy and safe failure

Prototype OS-level controls using Windows Firewall/WFP, supported application-control policy, and managed DNS. Prototype exact path rules only inside explicitly supported managed browsers. Begin in audit mode; test standard-user, IPv4/IPv6, Wi-Fi/Ethernet, sleep/resume, offline, browser updates, redirects, and approved accessibility/security tools. Measure false positives, coverage gaps, and recovery. Assess whether a custom WFP driver is necessary only after the supported control path has been evaluated and independently threat-modeled. Never disable OS security or terminate arbitrary processes.

**Gate:** each rule has test cases, event logging, parent-visible status, rollback, and plain-language explanation. Publish a coverage matrix naming supported configurations and known bypass limits; do not claim universal VPN/proxy or arbitrary-app URL-path blocking.

### Phase 3 — Customer installer

Create a signed Windows installer (EXE/MSI first; evaluate MSIX only if its service and policy requirements fit). Bundle the child agent runtime so families do not need Python or Node.js. Installer responsibilities: request admin consent, install the service and session companion, create unique device credentials, pair to the parent dashboard, set data paths/ACLs, configure only documented firewall rules, provide repair/uninstall, and preserve user data only when the parent explicitly chooses to.

Install a driver only if the Phase 2 gate proves it necessary and its security/release review passes. Do not modify Windows installation media/ISO for the first release. A custom image does not solve encrypted URL visibility or eliminate admin/network bypasses; use supported Windows provisioning if standardized device setup becomes necessary.

**Gate:** clean-machine install, reboot, upgrade, repair, uninstall, and rollback succeed without a developer toolchain or manually edited files.

### Phase 4 — Hardening and independent review

Threat-model the service, session app, IPC, pairing, update chain, local database, and dashboard API. Sign release binaries; secure update metadata; use dependency inventory and vulnerability tracking; remove secrets from the repository; enforce authorization on all sensitive APIs; define retention and deletion; prepare a software bill of materials. Commission an authorized independent security review before wider distribution. Fix findings and verify fixes before launch.

Do not call internal testing “no penetration” or “exploit-proof.” State what was reviewed, on which versions, and what remains outside scope.

### Phase 5 — Family pilot and release

Start with a small group of families on supported Windows PCs. Obtain clear administrator/guardian authorization and make monitoring status understandable to the child. Review usability, false positives, device performance, uninstall/recovery, and trust concerns. Only then decide on broader distribution, paid plans, more Windows editions, or remote/cloud access.

## 5. Prerequisites for you

### To build and release

- A Windows 11 development PC and clean Windows test machines/VMs. Start with x64; add ARM64 only after x64 is stable.
- Windows SDK/Visual Studio build tools; WDK only if the later driver gate is approved.
- Python and Node.js/npm for the current repository during development, plus supported dependencies and a reproducible build environment. Customers must not need these runtimes installed separately.
- A code-signing identity for distributed installers; a driver submission/signing path only if a custom driver is eventually approved.
- A parent dashboard machine on the same reachable network for the first release; a cloud service, DNS domain, secure operations, and ongoing hosting/security budget if remote access is later required.
- A standard Windows child account and parent-held admin account on every pilot device.
- Clear decisions on screen-view defaults, retention, supported browsers, Windows editions, offline behavior, and acceptable false positives.
- Privacy/legal review, support/recovery process, dependency/license review, update policy, and family-facing disclosure before public distribution.

### What AI can and cannot do

AI can inspect the repository, map callers and platform assumptions, propose changes, write packaging configuration, draft UI and documentation, generate focused tests, and review logs/diffs. AI cannot certify that the product is secure, establish legal authority, guarantee no exploit, sign code on your behalf, or prove a VPN/proxy cannot be used. A human must approve security boundaries, review changes, run them on real Windows machines, and own release decisions.

## 6. AI project workflow

Give the coding AI the repository root and this plan. Ask it to read files in this order:

1. `docs/README.md`, `docs/MASTER_AI.md`, and `docs/SESSION_MEMORY.md` for current architecture, rules, and state; then read this plan.
2. Inspect `GUARDIAN PC/design-reference/stitch_guardian_shield_parental_dashboard.zip` as data (list and read its HTML, screenshots, and `guardian_shield/DESIGN.md`; do not execute prototype scripts). Map each supplied page to the existing dashboard routes and actual product behaviors.
3. Read `GUARD.py`, `parent-dashboard/server-ts/index.js`, server routes/services, and `parent-dashboard/web-ts` to understand pairing, authentication, page navigation, and API/socket behavior.
4. Read `pi-agent/agent/main.py`, `app_tracker.py`, `screenshot.py`, `usage_tracker.py`, `browser_history.py`, `blocker.py`, `download_guard.py`, `persistence.py`, `consent.py`, and `requirements.txt` to map Windows behavior and privilege boundaries.
5. Read `windows-app/scripts/start-live-control.bat`, `install-server.bat`, and `windows-app/launcher/GuardianOneClick.cs` to distinguish demo/setup code from product packaging.

Then have AI produce a **read-only Windows support matrix and data-flow map first**. It must identify platform branches, imports that fail on Windows, privilege needs, service/session boundaries, secrets, data locations, and every caller before proposing edits. Require file/line evidence for claims.

Implement in small checkpoints, one concern per change: (a) map and wire supplied Stitch UI into the current web-ts app, (b) Windows parity fixes, (c) service/session split, (d) pairing and secure local storage, (e) WFP/firewall and application-control integration in audit mode, (f) managed-browser URL-path policy and route coverage, (g) installer/update/uninstall, (h) evaluate a custom driver only if evidence requires it, (i) pilot hardening. At each checkpoint, AI should show files changed, privilege boundary, security impact, compatibility, rollback, and verification evidence. Do not accept stealth mode, credential/key logging, disabling Windows protections, or claims such as “unhackable.” Run tests only when requested or when a change plan explicitly approves them; hardware/session, installer, and policy behavior still requires manual verification on clean Windows devices.

Suggested first AI task:

> Read the project instructions and the listed Windows agent/server files. Do not edit anything. Return a Windows-only feature-support matrix, startup/data-flow map, all Windows-specific blockers with file/line references, privilege requirements, and a proposed service/session split. Compare Windows-supported WFP/firewall/application-control enforcement with a custom WFP callout only where a documented gap remains. Include a concrete design for blocking YouTube Shorts paths while allowing normal YouTube in each proposed managed browser, and state where only domain-level blocking is possible. Treat child visibility, data minimization, least privilege, rollback, and recovery as requirements. Do not propose stealth behavior, bypassing Windows security, arbitrary file collection, TLS interception, or a guarantee that all VPNs/proxies can be defeated. Mark uncertainty and what evidence would resolve it.

## 7. Open decisions for the product owner

These are deliberate decision gates, not assumptions for an AI to fill in:

1. Is the first target a **home-network product** where the parent dashboard runs on another household PC, or must parents reach it remotely from outside the home? Remote access changes hosting, authentication, operations, privacy, and cost substantially.
2. This plan assumes the parent can open a live view whenever the device is online and an interactive child session is available. The session indicator remains visible; offline, logged-out, locked, and secure-desktop states are reported honestly, and screenshot history is off by default.
3. Which Windows editions and browsers must the first release support? App-control capabilities and behavior vary by Windows edition and configuration.
4. Will every child use a standard account with the parent holding administrator credentials? Without that, strong local policy enforcement is not realistic.
5. Which files, if any, are in scope for a scan? The default should be no arbitrary file collection or upload.

## 8. References

- [Windows Filtering Platform overview](https://learn.microsoft.com/en-us/windows/win32/fwp/about-windows-filtering-platform)
- [Application Control for Windows](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/appcontrol)
- [Windows service and interactive desktop guidance](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)
- [Kernel-mode signing requirements](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/kernel-mode-code-signing-requirements--windows-vista-and-later-)
- [Public driver release signing](https://learn.microsoft.com/en-us/windows-hardware/drivers/develop/signing-a-driver-for-public-release)
