// markdown-it ships no types. This declares only what MarkdownView uses.
declare module "markdown-it" {
  interface Token {
    readonly children: Token[] | null;
    attrGet(name: string): string | null;
    attrSet(name: string, value: string): void;
  }
  interface Renderer {
    readonly rules: { image: Rule };
    renderInlineAsText(tokens: Token[], options: unknown, env: unknown): string;
  }
  type Rule = (tokens: Token[], index: number, options: unknown, env: unknown, self: Renderer) => string;

  export default class MarkdownIt {
    constructor(options?: Readonly<{ html?: boolean; linkify?: boolean; typographer?: boolean }>);
    readonly renderer: Renderer;
    readonly utils: { escapeHtml(text: string): string };
    render(source: string): string;
  }
}
