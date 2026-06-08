import * as React from "react";

/**
 * Labelled text field with optional leading icon, hint and error.
 * @startingPoint section="Forms" subtitle="Text field with label, icon, hint, error" viewport="700x180"
 */
export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  /** Field label rendered above the input. */
  label?: string;
  /** Helper text below the field. */
  hint?: string;
  /** Error message — turns the field red and replaces the hint. */
  error?: string;
  /** Show a required asterisk on the label. @default false */
  required?: boolean;
  /** Icon node rendered inside the field, left-aligned. */
  leadingIcon?: React.ReactNode;
  /** Use the monospace family (for selectors, IDs, paths). @default false */
  mono?: boolean;
}

export declare function Input(props: InputProps): JSX.Element;
