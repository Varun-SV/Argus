import * as React from "react";

export interface SelectOption {
  value: string;
  label: string;
}

/**
 * Styled native select with a custom chevron.
 */
export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  /** Field label rendered above the control. */
  label?: string;
  /** Helper text below the control. */
  hint?: string;
  /** Options to render. Omit and pass <option> children instead if preferred. */
  options?: SelectOption[];
}

export declare function Select(props: SelectProps): JSX.Element;
