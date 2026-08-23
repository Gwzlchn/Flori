declare module "markdown-it" {
  type MarkdownItToken = { content: string };
  export type MarkdownItOptions = Readonly<{
    breaks?: boolean;
    html?: boolean;
    linkify?: boolean;
    typographer?: boolean;
  }>;

  export default class MarkdownIt {
    constructor(options?: MarkdownItOptions);
    readonly renderer: {
      rules: { text?: (tokens: MarkdownItToken[], index: number) => string };
    };
    readonly utils: { escapeHtml(value: string): string };
    render(source: string): string;
  }
}
