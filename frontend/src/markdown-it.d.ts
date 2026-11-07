// markdown-it ships no types. This declares only what MarkdownView uses.
declare module "markdown-it" {
  interface Token {
    attrGet(name: string): string | null;
    attrSet(name: string, value: string): void;
  }
  type Rule = (tokens: Token[], index: number, options: unknown, env: unknown, self: unknown) => string;

  export default class MarkdownIt {
    constructor(options?: Readonly<{ html?: boolean; linkify?: boolean; typographer?: boolean }>);
    readonly renderer: { rules: { image: Rule } };
    render(source: string): string;
  }
}
