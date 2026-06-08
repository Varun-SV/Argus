import * as React from "react";

/**
 * Monospace chip for adapters, providers, environments and labels.
 */
export interface TagProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** Icon node rendered before the label (~13px). */
  icon?: React.ReactNode;
  /** Render a colored leading dot of this CSS color. */
  dotColor?: string;
  /** Show a remove "×"; called when clicked. */
  onRemove?: (e: React.MouseEvent) => void;
  /** Apply hover affordance even without onClick. @default false */
  interactive?: boolean;
}

export declare function Tag(props: TagProps): JSX.Element;
