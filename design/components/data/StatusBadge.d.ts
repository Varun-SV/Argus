import * as React from "react";

/**
 * The canonical representation of a test outcome across every Argus surface.
 * @startingPoint section="Data" subtitle="Test outcome badge: pass/fail/error/running/flaky" viewport="700x140"
 */
export interface StatusBadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** The test outcome. @default "pass" */
  status?: "pass" | "fail" | "error" | "running" | "skipped" | "flaky";
  /** Override the default label text ("Passed", "Failed", …). */
  label?: string;
  /** Solid fill instead of tinted (use for emphasis / summary headers). @default false */
  solid?: boolean;
}

export declare function StatusBadge(props: StatusBadgeProps): JSX.Element;
