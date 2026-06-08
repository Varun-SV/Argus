import * as React from "react";

/**
 * Labelled boolean checkbox with a custom check glyph.
 */
export interface CheckboxProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  /** Controlled checked state. */
  checked?: boolean;
  /** Uncontrolled initial state. */
  defaultChecked?: boolean;
  /** Trailing label text. */
  label?: string;
  /** Disable interaction. @default false */
  disabled?: boolean;
}

export declare function Checkbox(props: CheckboxProps): JSX.Element;
