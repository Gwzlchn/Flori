// markdown-it ships no types. This declares only what MarkdownView uses.
declare module "markdown-it" {
  class Token {
    constructor(type: string, tag: string, nesting: number);
    readonly type: string;
    content: string;
    children: Token[] | null;
    attrGet(name: string): string | null;
    attrSet(name: string, value: string): void;
  }
  interface Renderer {
    readonly rules: { image: Rule; [name: string]: Rule };
    renderInlineAsText(tokens: Token[], options: unknown, env: unknown): string;
  }
  type Rule = (tokens: Token[], index: number, options: unknown, env: unknown, self: Renderer) => string;
  interface CoreState {
    readonly tokens: Token[];
    readonly Token: typeof Token;
  }

  export default class MarkdownIt {
    constructor(options?: Readonly<{ html?: boolean; linkify?: boolean; typographer?: boolean }>);
    readonly renderer: Renderer;
    readonly core: { ruler: { push(name: string, rule: (state: CoreState) => void): void } };
    readonly utils: { escapeHtml(text: string): string };
    render(source: string): string;
    use<T>(plugin: (md: MarkdownIt, options?: T) => unknown, options?: T): this;
  }
}
