/**
 * Seeded request behind the PDF figure "digitize" action on a visual mark.
 *
 * The composer activates a skill through an inline token plus the derived
 * `selectedSkillIds` state, so the seeded prompt carries the encoded skill
 * token (and the paper mention) rather than restating provider `$` / `/`
 * syntax, which stays Host-owned — the same rule paper-reader follows.
 *
 * The body is English like the other agent-facing scaffolds (PDF ask, quoted
 * selections); the surrounding button label is i18n'd in `viewer.json`.
 */

import {
	encodeMentionToken,
	encodeSkillToken,
} from "@/lib/agent/composer-inline-tokens";

export const FIGURE_DIGITIZER_SKILL_ID = "figure-digitizer";

/** Vault-relative paper folder, or "" for absolute paths / crop fallbacks. */
function vaultPaperPath(paperPath: string): string {
	const trimmed = paperPath
		.trim()
		.replace(/\\/g, "/")
		.replace(/^\/+|\/+$/g, "");
	if (!trimmed || trimmed === "paper") return "";
	return trimmed;
}

export function buildFigureDigitizePrompt(input: {
	/** Vault-relative paper folder, e.g. `papers/1706.03762`. */
	paperPath: string;
	/** 1-based PDF page the crop was taken from. */
	page: number;
}): string {
	const paper = vaultPaperPath(input.paperPath);
	const tokens = paper
		? `${encodeSkillToken(FIGURE_DIGITIZER_SKILL_ID)} ${encodeMentionToken(paper)}`
		: encodeSkillToken(FIGURE_DIGITIZER_SKILL_ID);
	const where = paper
		? `the figure crop attached below, from \`${paper}\` page ${input.page}`
		: `the figure crop attached below (page ${input.page})`;
	return [
		`${tokens} Digitize ${where}.`,
		"Preflight first: confirm the panel, the chart type and both axis calibrations with me before measuring — a proposed route never authorises numbers.",
		"Then extract against the original raster and write data.csv, overlay.png, recreated.png and report.json under the paper's `source/digitize/<figure-id>/` folder, and show me the overlay so I can check the marks.",
		"Marks that are occluded, merged or uncalibratable stay low_confidence / not_extracted; never interpolate or guess.",
	].join("\n");
}
