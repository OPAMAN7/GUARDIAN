# Pi IP selection and network recalibration

Date: 2026-10-21

## Problem

The previous recalibration routine tried the cached address, then resolved the configured hostname, and then scanned every local /24 for any host accepting SSH on port 22. An unrelated SSH server could therefore be printed as “Pi found.” In the reported run GUARD selected 10.248.254.218, while the Pi reports 192.168.1.15 from hostname -I.

## Decision

- Ask which Pi IP to use before making any Pi network probe.
- A blank answer or auto means probe only GUARD's cached last_pi_ip. Do not resolve the hostname, scan subnets, or fall back to another address.
- A manually entered address must be a valid IPv4 address and is the only target probed in that run.
- Retry the selected IP a few times for a settling network, but never substitute another SSH host.
- After successful recalibration, save the selected Pi address as the new cached IP.
- Make quick checks used by diagnostics and data pull use only the cached Pi IP.
- Keep GUARD.bat pi-ip <addr> as a direct way to update GUARD's cached target.

## Use

At the recalibration prompt, enter 192.168.1.15 for the address reported by the Pi, or enter auto to use the currently cached address. If auto fails, the run stops at that address and tells the user; it does not look elsewhere.

The Pi's actual Wi-Fi/DHCP address remains assigned by the router or configured on the Pi. GUARD's prompt and pi-ip command only select which address GUARD contacts.

## Verification

Python syntax parsing passed. Mocked checks passed for manual mode probing only the entered address, auto probing only the cached address, and quick checks never resolving or scanning other hosts. A live SSH authentication, service restart, and Pi-to-dashboard check still needs to be completed against the user's Pi.
