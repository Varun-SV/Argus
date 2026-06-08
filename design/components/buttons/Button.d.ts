import * as React from "react";

/**
 * Primary action control for Argus surfaces.
 * @startingPoint section="Core" subtitle="Button with variants, sizes, icons, loading" viewport="700x200"
 */
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual emphasis. @default "primary" */
  variant?: "primary" | "secondary" | "ghost" | "danger";
  /** Control height. @default "md" */
  size?: "sm" | "md" | "lg";
  /** Icon node rendered before the label (16px). */
  leftIcon?: React.ReactNode;
  /** Icon node rendered after the label (16px). */
  rightIcon?: React.ReactNode;
  /** Show a spinner and block interaction. @default false */
  loading?: boolean;
  /** Stretch to full width. @default false */
  block?: boolean;
}

export declare function Button(props: ButtonProps): JSX.Element;
