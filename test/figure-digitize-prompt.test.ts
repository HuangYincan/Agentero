import { describe, expect, it } from "vitest";
import {
	deriveComposerTokens,
	stripInlineTokens,
} from "@/lib/agent/composer-inline-tokens";
import {
	buildFigureDigitizePrompt,
	FIGURE_DIGITIZER_SKILL_ID,
} from "@/lib/pdf-visual/digitize-prompt";

describe("figure digitize handoff", () => {
	it("activates the figure-digitizer skill and mentions the paper", () => {
		const prompt = buildFigureDigitizePrompt({
			paperPath: "papers/1706.03762",
			page: 4,
		});
		// The send path reads the derived state, not the raw text: a seeded
		// prompt only reaches the run when both agree.
		const { selectedSkillIds, mentionedPaths } = deriveComposerTokens(prompt);
		expect(selectedSkillIds).toEqual([FIGURE_DIGITIZER_SKILL_ID]);
		expect(mentionedPaths).toEqual(["papers/1706.03762"]);
	});

	it("keeps the skill active when the paper path is unusable", () => {
		for (const paperPath of ["", "   ", "/", "paper", "///"]) {
			const { selectedSkillIds, mentionedPaths } = deriveComposerTokens(
				buildFigureDigitizePrompt({ paperPath, page: 2 }),
			);
			expect(selectedSkillIds).toEqual([FIGURE_DIGITIZER_SKILL_ID]);
			expect(mentionedPaths).toEqual([]);
		}
	});

	it("normalizes a slash- or backslash-wrapped paper path", () => {
		const { mentionedPaths } = deriveComposerTokens(
			buildFigureDigitizePrompt({
				paperPath: "/papers\\1706.03762/",
				page: 1,
			}),
		);
		expect(mentionedPaths).toEqual(["papers/1706.03762"]);
	});

	it("reads as prose once the tokens are stripped, and names the page", () => {
		const prompt = buildFigureDigitizePrompt({
			paperPath: "papers/1706.03762",
			page: 7,
		});
		const prose = stripInlineTokens(prompt);
		expect(prose).not.toContain("{{");
		expect(prose).toContain("page 7");
		expect(prose).toContain("papers/1706.03762");
	});

	it("asks for preflight confirmation before any number is produced", () => {
		const prose = stripInlineTokens(
			buildFigureDigitizePrompt({
				paperPath: "papers/x",
				page: 1,
			}),
		);
		expect(prose).toMatch(/preflight/i);
		expect(prose).toMatch(/before measuring/i);
		expect(prose).toMatch(/low_confidence/);
	});
});
