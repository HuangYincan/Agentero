"use client";

import type { TElement } from "platejs";
import { PlateElement, type PlateElementProps } from "platejs/react";
import * as React from "react";
import { ImageGroupContext } from "@/components/editor/context/image-group-context";

type TImageGroupChild = TElement & { url?: string };

/**
 * 飞书式图片组:子图片单行并排、自动等高。宽度按各图宽高比分配
 * (`flex: r 1 0%` + `aspect-ratio: r` 使高度收敛到同一行高),放不下时
 * flex-wrap 降级换行。宽高比是展示期状态,不写入文档。
 */
export function ImageGroupElement(props: PlateElementProps<TElement>) {
	const items = props.element.children as TImageGroupChild[];
	const [ratios, setRatios] = React.useState<Record<string, number>>({});
	const reportRatio = React.useCallback((url: string, ratio: number) => {
		setRatios((prev) =>
			prev[url] === ratio ? prev : { ...prev, [url]: ratio },
		);
	}, []);

	return (
		<ImageGroupContext.Provider value={{ reportRatio }}>
			<PlateElement {...props} className="flex flex-wrap gap-2 py-2">
				{React.Children.map(props.children, (child, index) => {
					const url = items[index]?.url;
					const ratio = url ? ratios[url] : undefined;
					return <ImageGroupItem ratio={ratio}>{child}</ImageGroupItem>;
				})}
			</PlateElement>
		</ImageGroupContext.Provider>
	);
}

function ImageGroupItem({
	ratio,
	children,
}: {
	ratio?: number;
	children: React.ReactNode;
}) {
	// grow=比例 → 宽度按宽高比分配;aspect-ratio=比例 → 各项高度一致。
	// 加载前没有比例:均分占位,给个最小高度保持组的形状。
	const style: React.CSSProperties =
		ratio != null && Number.isFinite(ratio) && ratio > 0
			? { aspectRatio: `${ratio}`, flex: `${ratio} 1 0%` }
			: { flex: "1 1 0%", minHeight: "4rem" };
	return (
		<div className="relative m-0 min-w-[72px]" style={style}>
			{children}
		</div>
	);
}
