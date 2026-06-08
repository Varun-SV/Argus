import * as React from "react";

/**
 * Small count or neutral label.
 */
export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** Color treatment. @default "neutral" */
  variant?: "neutral" | "brand" | "accent" | "solid";
}

export declare function Badge(props: BadgeProps): JSX.Element;
