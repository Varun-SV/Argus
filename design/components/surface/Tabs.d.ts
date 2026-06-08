import * as React from "react";

export interface TabItem {
  /** Unique id used as the controlled value. */
  id: string;
  /** Visible label. */
  label: React.ReactNode;
  /** Optional leading icon (~15px). */
  icon?: React.ReactNode;
  /** Optional trailing count. */
  count?: number;
}

/**
 * Horizontal tab navigation — underline or segmented pill style.
 * @startingPoint section="Navigation" subtitle="Underline & segmented pill tabs" viewport="700x120"
 */
export interface TabsProps {
  /** Tabs to render. */
  items: TabItem[];
  /** Currently-selected tab id. */
  value: string;
  /** Called with the next tab id. */
  onChange?: (id: string) => void;
  /** Visual style. @default "underline" */
  variant?: "underline" | "pills";
}

export declare function Tabs(props: TabsProps): JSX.Element;
