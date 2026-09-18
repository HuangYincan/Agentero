import { MarkdownPlugin } from "@platejs/markdown";
import { createSlateEditor, createSlatePlugin, KEYS } from "platejs";
import { describe, expect, it } from "vitest";
import { ImageGroupPlugin } from "@/components/editor/plugins/image-group-plugin";
import { MarkdownKit } from "@/components/editor/plugins/markdown-kit";
import {
	IMAGE_GROUP_KEY,
	imageGroupRules,
	MAX_IMAGE_GROUP_SIZE,
	remarkImageGroup,
} from "@/lib/markdown/image-group";

const TestParagraphPlugin = createSlatePlugin({
	key: KEYS.p,
	node: { isElement: true },
});
const TestBlockquotePlugin = createSlatePlugin({
	key: KEYS.blockquote,
	node: { isElement: true },
});
const TestImagePlugin = createSlatePlugin({
	key: KEYS.img,
	node: { isElement: true, isVoid: true },
});
const TestImageGroupPlugin = createSlatePlugin({
	key: IMAGE_GROUP_KEY,
	node: { isElement: true },
});
const TestMarkdownPlugin = MarkdownPlugin.configure({
	options: {
		remarkPlugins: [remarkImageGroup],
		rules: { ...imageGroupRules },
	},
});

function createImageGroupEditor(markdown: string) {
	return createSlateEditor({
		plugins: [
			TestParagraphPlugin,
			TestBlockquotePlugin,
			TestImagePlugin,
			TestImageGroupPlugin,
			TestMarkdownPlugin,
		],
		value: (editor) =>
			editor.getApi(MarkdownPlugin).markdown.deserialize(markdown),
	});
}

describe("image group Markdown model", () => {
	it("folds adjacent image lines into one group with url and alt", () => {
		const editor = createImageGroupEditor(
			"![a](./assets/a.png)\n![b](./assets/b.png)",
		);

		expect(editor.children).toMatchObject([
			{
				type: IMAGE_GROUP_KEY,
				children: [
					{
						type: "img",
						url: "./assets/a.png",
						caption: [{ text: "a" }],
						children: [{ text: "" }],
					},
					{
						type: "img",
						url: "./assets/b.png",
						caption: [{ text: "b" }],
						children: [{ text: "" }],
					},
				],
			},
		]);
	});

	it("keeps blank-line separated runs as two groups", () => {
		const editor = createImageGroupEditor(
			"![](a.png)\n![](b.png)\n![](c.png)\n\n![](d.png)\n![](e.png)",
		);

		expect(editor.children).toMatchObject([
			{
				type: IMAGE_GROUP_KEY,
				children: [{ url: "a.png" }, { url: "b.png" }, { url: "c.png" }],
			},
			{
				type: IMAGE_GROUP_KEY,
				children: [{ url: "d.png" }, { url: "e.png" }],
			},
		]);
	});

	it("leaves a single image line as a standalone image block", () => {
		const editor = createImageGroupEditor("![](a.png)");

		expect(editor.children).toMatchObject([{ type: "img", url: "a.png" }]);
	});

	it("does not merge images separated by a blank line", () => {
		const editor = createImageGroupEditor("![](a.png)\n\n![](b.png)");

		expect(editor.children).toMatchObject([
			{ type: "img", url: "a.png" },
			{ type: "img", url: "b.png" },
		]);
	});

	it("does not fold a mixed image/text line", () => {
		const editor = createImageGroupEditor("![](a.png)\nsee this");

		expect(editor.children).toMatchObject([
			{ type: "img", url: "a.png" },
			{ type: "p" },
		]);
		expect(editor.children.some((node) => node.type === IMAGE_GROUP_KEY)).toBe(
			false,
		);
	});

	it("serializes a group back to adjacent image lines", () => {
		const editor = createImageGroupEditor(
			"![a](./assets/a.png)\n![b](./assets/b.png)",
		);

		expect(editor.getApi(MarkdownPlugin).markdown.serialize()).toBe(
			"![a](./assets/a.png)\n![b](./assets/b.png)\n",
		);
	});

	it("round-trips groups with alt, title, and surrounding paragraphs", () => {
		const markdown =
			'Before\n\n![](a.png "the title")\n![alt b](b.png)\n\nAfter';
		const editor = createImageGroupEditor(markdown);

		expect(editor.getApi(MarkdownPlugin).markdown.serialize()).toBe(
			`${markdown}\n`,
		);
	});

	it("round-trips two groups separated by a blank line", () => {
		const markdown = "![](a.png)\n![](b.png)\n\n![](c.png)\n![](d.png)";
		const editor = createImageGroupEditor(markdown);

		expect(editor.getApi(MarkdownPlugin).markdown.serialize()).toBe(
			`${markdown}\n`,
		);
	});

	it("leaves adjacent images inside a blockquote untouched", () => {
		const editor = createImageGroupEditor("> ![](a.png)\n> ![](b.png)");

		expect(editor.children).toMatchObject([{ type: "blockquote" }]);
		const serialized = editor.getApi(MarkdownPlugin).markdown.serialize();
		expect(serialized).toContain("![](a.png)");
		expect(serialized).toContain("![](b.png)");
	});

	it("uses the production Markdown kit for image groups", () => {
		const editor = createSlateEditor({
			plugins: [
				TestParagraphPlugin,
				TestImagePlugin,
				TestImageGroupPlugin,
				...MarkdownKit,
			],
			value: (currentEditor) =>
				currentEditor
					.getApi(MarkdownPlugin)
					.markdown.deserialize("![](a.png)\n![](b.png)"),
		});

		expect(editor.children).toMatchObject([
			{ type: IMAGE_GROUP_KEY, children: [{ url: "a.png" }, { url: "b.png" }] },
		]);
		expect(editor.getApi(MarkdownPlugin).markdown.serialize()).toBe(
			"![](a.png)\n![](b.png)\n",
		);
	});
});

function imageEl(url: string) {
	return { type: "img", url, children: [{ text: "" }] };
}

function urlsOf(element: unknown): (string | undefined)[] {
	const node = element as {
		type?: string;
		url?: string;
		children?: { url?: string }[];
	};
	if (node.type === "img") return [node.url];
	return node.children?.map((child) => child.url) ?? [];
}

function createNormalizeEditor(value: unknown[]) {
	const editor = createSlateEditor({
		plugins: [TestParagraphPlugin, TestImagePlugin, ImageGroupPlugin],
		value: value as never,
	});
	editor.tf.normalize({ force: true });
	return editor;
}

describe("image group normalize", () => {
	it("splits a group larger than the max size", () => {
		const urls = Array.from(
			{ length: MAX_IMAGE_GROUP_SIZE + 1 },
			(_, i) => `i${i}.png`,
		);
		const editor = createNormalizeEditor([
			{ type: IMAGE_GROUP_KEY, children: urls.map(imageEl) },
		]);

		expect(editor.children.map((node) => node.type)).toEqual([
			IMAGE_GROUP_KEY,
			"img",
		]);
		expect(urlsOf(editor.children[0])).toEqual(
			urls.slice(0, MAX_IMAGE_GROUP_SIZE),
		);
		expect(urlsOf(editor.children[1])).toEqual([urls[MAX_IMAGE_GROUP_SIZE]]);
	});

	it("splits a twelve-image group into ten and two", () => {
		const urls = Array.from({ length: 12 }, (_, i) => `i${i}.png`);
		const editor = createNormalizeEditor([
			{ type: IMAGE_GROUP_KEY, children: urls.map(imageEl) },
		]);

		expect(urlsOf(editor.children[0])).toEqual(urls.slice(0, 10));
		expect(urlsOf(editor.children[1])).toEqual(urls.slice(10));
	});

	it("merges adjacent groups up to the max size and keeps oversized pairs", () => {
		const merged = createNormalizeEditor([
			{
				type: IMAGE_GROUP_KEY,
				children: ["a.png", "b.png", "c.png"].map(imageEl),
			},
			{
				type: IMAGE_GROUP_KEY,
				children: ["d.png", "e.png", "f.png", "g.png"].map(imageEl),
			},
		]);
		expect(merged.children).toHaveLength(1);
		expect(urlsOf(merged.children[0])).toEqual([
			"a.png",
			"b.png",
			"c.png",
			"d.png",
			"e.png",
			"f.png",
			"g.png",
		]);

		const kept = createNormalizeEditor([
			{
				type: IMAGE_GROUP_KEY,
				children: Array.from({ length: 7 }, (_, i) => imageEl(`l${i}.png`)),
			},
			{
				type: IMAGE_GROUP_KEY,
				children: Array.from({ length: 7 }, (_, i) => imageEl(`r${i}.png`)),
			},
		]);
		expect(kept.children.map((node) => node.type)).toEqual([
			IMAGE_GROUP_KEY,
			IMAGE_GROUP_KEY,
		]);
		expect(urlsOf(kept.children[0])).toEqual(
			Array.from({ length: 7 }, (_, i) => `l${i}.png`),
		);
		expect(urlsOf(kept.children[1])).toEqual(
			Array.from({ length: 7 }, (_, i) => `r${i}.png`),
		);
	});

	it("dissolves a group reduced to one image", () => {
		const editor = createNormalizeEditor([
			{ type: IMAGE_GROUP_KEY, children: [imageEl("a.png")] },
		]);

		expect(editor.children).toMatchObject([{ type: "img", url: "a.png" }]);
	});

	it("absorbs a neighboring image but not into a full group", () => {
		const absorb = createNormalizeEditor([
			{
				type: IMAGE_GROUP_KEY,
				children: ["a.png", "b.png", "c.png"].map(imageEl),
			},
			imageEl("d.png"),
		]);
		expect(absorb.children).toHaveLength(1);
		expect(urlsOf(absorb.children[0])).toEqual([
			"a.png",
			"b.png",
			"c.png",
			"d.png",
		]);

		const urls = Array.from(
			{ length: MAX_IMAGE_GROUP_SIZE },
			(_, i) => `f${i}.png`,
		);
		const full = createNormalizeEditor([
			{ type: IMAGE_GROUP_KEY, children: urls.map(imageEl) },
			imageEl("extra.png"),
		]);
		expect(full.children.map((node) => node.type)).toEqual([
			IMAGE_GROUP_KEY,
			"img",
		]);
		expect(urlsOf(full.children[0])).toEqual(urls.map((u) => `${u}`));
	});

	it("drops empty groups and lifts foreign children out", () => {
		const empty = createNormalizeEditor([
			{ type: IMAGE_GROUP_KEY, children: [] },
			imageEl("a.png"),
		]);
		expect(empty.children).toMatchObject([{ type: "img", url: "a.png" }]);

		const foreign = createNormalizeEditor([
			{
				type: IMAGE_GROUP_KEY,
				children: [
					imageEl("a.png"),
					{ type: "p", children: [{ text: "text" }] },
				],
			},
		]);
		// lift 落在组后;组随后因只剩一张而解散,相对顺序保持 [img, p]。
		expect(foreign.children).toMatchObject([
			{ type: "img", url: "a.png" },
			{ type: "p", children: [{ text: "text" }] },
		]);
	});

	it("wraps adjacent images but not across a paragraph", () => {
		const wrapped = createNormalizeEditor([imageEl("a.png"), imageEl("b.png")]);
		expect(wrapped.children).toHaveLength(1);
		expect(wrapped.children[0]).toMatchObject({ type: IMAGE_GROUP_KEY });
		expect(urlsOf(wrapped.children[0])).toEqual(["a.png", "b.png"]);

		const split = createNormalizeEditor([
			imageEl("a.png"),
			{ type: "p", children: [{ text: "text" }] },
			imageEl("b.png"),
		]);
		expect(split.children.map((node) => node.type)).toEqual([
			"img",
			"p",
			"img",
		]);
	});

	it("splits an over-sized run coming from markdown", () => {
		const lines = Array.from(
			{ length: MAX_IMAGE_GROUP_SIZE + 1 },
			(_, i) => `![](i${i}.png)`,
		);
		const editor = createSlateEditor({
			plugins: [
				TestParagraphPlugin,
				TestImagePlugin,
				TestMarkdownPlugin,
				ImageGroupPlugin,
			],
			value: (currentEditor) =>
				currentEditor
					.getApi(MarkdownPlugin)
					.markdown.deserialize(lines.join("\n")),
		});
		editor.tf.normalize({ force: true });

		expect(editor.children.map((node) => node.type)).toEqual([
			IMAGE_GROUP_KEY,
			"img",
		]);
		// 拆分后的两个顶层块以空行分隔,重新解析仍为「组 + 单图」稳定形态。
		expect(editor.getApi(MarkdownPlugin).markdown.serialize()).toBe(
			`${lines.slice(0, MAX_IMAGE_GROUP_SIZE).join("\n")}\n\n${lines[MAX_IMAGE_GROUP_SIZE]}\n`,
		);
	});
});
