import { isTauri } from "@/lib/core/tauri";

/** Close the current Tauri window; no-op outside Tauri. Best-effort. */
export function closeCurrentWindow(): void {
	if (!isTauri()) return;
	void (async () => {
		try {
			const { getCurrentWindow } = await import("@tauri-apps/api/window");
			await getCurrentWindow().close();
		} catch {
			// ignore
		}
	})();
}
