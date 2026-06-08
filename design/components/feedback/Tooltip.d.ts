import * as React from "react";

/**
 * Hover/focus tooltip that wraps a single trigger element.
 */
export interface TooltipProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** Tooltip body text/content. */
  content: React.ReactNode;
  /** Optional keyboard shortcut shown muted on the right (e.g. "⌘R"). */
  kbd?: string;
  /** The trigger element. */
  children: React.ReactNode;
}

export declare function Tooltip(props: TooltipProps): JSX.Element;
