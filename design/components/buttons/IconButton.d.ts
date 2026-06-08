import * as React from "react";

/**
 * Square, icon-only control for toolbars and dense UI.
 */
export interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Control size. @default "md" */
  size?: "sm" | "md";
  /** "ghost" is transparent; "solid" has a raised surface. @default "ghost" */
  variant?: "ghost" | "solid";
  /** Active/pressed (e.g. a toggled toolbar tool). @default false */
  active?: boolean;
  /** Accessible label + tooltip text (icon-only buttons need this). */
  label?: string;
}

export declare function IconButton(props: IconButtonProps): JSX.Element;
