import * as React from "react";

/**
 * Boolean toggle with optional trailing label.
 */
export interface SwitchProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  /** Controlled checked state. */
  checked?: boolean;
  /** Uncontrolled initial state. */
  defaultChecked?: boolean;
  /** Trailing label text. */
  label?: string;
  /** Disable interaction. @default false */
  disabled?: boolean;
}

export declare function Switch(props: SwitchProps): JSX.Element;
