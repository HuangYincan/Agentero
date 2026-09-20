import { isMacOS, isMobileApp, isTauri } from "@/lib/core/tauri";
import type { ThemePreference } from "@/lib/settings";

let applied: ThemePreference | null = null;

/**
 * Mirror the theme preference onto the window's native chrome.
 *
 * Windows and Linux draw the caption bar themselves, so it follows the OS app
 * mode unless the window is told otherwise — a dark app keeps a white title bar
 * while Windows is in light mode. `set_theme` maps to the Windows immersive
 * dark title bar; `system` passes `null` so the caption keeps following the OS.
 *
 * macOS is skipped: its Overlay title bar renders over the themed React header
 * already, and `set_theme` there overrides the app-wide `NSAppearance`.
 */
export function applyNativeWindowTheme(preference: ThemePreference): void {
	if (!isTauri() || isMobileApp() || isMacOS()) return;
	if (applied === preference) return;
	applied = preference;
	void import("@tauri-apps/api/window")
		.then(({ getCurrentWindow }) =>
			getCurrentWindow().setTheme(preference === "system" ? null : preference),
		)
		.catch(() => {
			// Unsupported platform / missing permission: keep the OS caption.
		});
}
