import * as React from "react";

/**
 * Surface container with optional Header / Body / Footer subcomponents.
 * @startingPoint section="Surface" subtitle="Container with header, body, footer" viewport="700x240"
 */
export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Raised surface + stronger shadow. @default false */
  raised?: boolean;
  /** Hover lift + pointer (use for clickable list cards). @default false */
  interactive?: boolean;
}

export interface CardHeaderProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Title text rendered at display weight. */
  title?: React.ReactNode;
  /** Right-aligned action nodes (buttons, menus). */
  actions?: React.ReactNode;
}

export declare function Card(props: CardProps): JSX.Element;
export declare namespace Card {
  function Header(props: CardHeaderProps): JSX.Element;
  function Body(props: React.HTMLAttributes<HTMLDivElement>): JSX.Element;
  function Footer(props: React.HTMLAttributes<HTMLDivElement>): JSX.Element;
}
