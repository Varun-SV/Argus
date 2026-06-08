import * as React from "react";

export interface ProgressSegment {
  /** Color band. */
  kind: "pass" | "fail" | "error" | "skip";
  /** Relative size (counts are fine; the bar normalizes). */
  value: number;
}

/**
 * Single-value bar, indeterminate running shimmer, or segmented distribution.
 */
export interface ProgressBarProps {
  /** Filled percentage 0–100 (ignored when `segments` or `running`). @default 0 */
  value?: number;
  /** Segmented pass/fail/error/skip distribution. */
  segments?: ProgressSegment[];
  /** Indeterminate shimmering "running" state. @default false */
  running?: boolean;
  /** 4px tall instead of 8px. @default false */
  thin?: boolean;
}

export declare function ProgressBar(props: ProgressBarProps): JSX.Element;
