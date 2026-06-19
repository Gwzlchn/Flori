/** Mnemo 设计 token → Tailwind 主题
 *  与 src/assets/mnemo.css 的 :root 变量一一对应。
 *  说明:本工程样式主要走 mnemo.css 的组件类（.btn/.card/.badge…）；
 *  这里的 token 供需要用 Tailwind 工具类时取用（如 text-ink-700 / bg-brand-600）。
 */
export default {
  content: ['./index.html', './src/**/*.{vue,ts,js}'],
  theme: {
    extend: {
      colors: {
        brand: { 50:'#eaf3fb',100:'#d3e6f8',200:'#a8cef0',300:'#6fb0e8',500:'#2f93e0',600:'#2383e2',700:'#1a6fc4' },
        ink:   { 900:'#37352f',800:'#4a4843',700:'#605e57',600:'#787670',500:'#9b9a94',400:'#bcbbb5',300:'#d6d5d0',200:'#e7e6e2' },
        line:  { DEFAULT:'#ecebe8', soft:'#f4f3f0' },
        surface:'#ffffff', bg:'#f7f7f5', raised:'#fbfbfa',
        // 语义状态色（覆盖 Job/Step/Worker/Concept/Collection 枚举）
        ok:'#3f8f48', info:'#2383e2', run:'#2383e2', warn:'#cb7b1f', bad:'#d9484b', mut:'#787670', amber:'#dd9b3a',
        // 内容类型
        'type-video':'#2383e2', 'type-paper':'#9065b0', 'type-article':'#448361', 'type-audio':'#d9730d',
      },
      borderRadius: { sm:'5px', DEFAULT:'8px', md:'8px', lg:'12px' },
      boxShadow: {
        sm:'0 1px 2px rgba(15,15,15,.05)',
        md:'0 3px 10px rgba(15,15,15,.07)',
        lg:'0 14px 44px rgba(15,15,15,.18)',
      },
      fontFamily: {
        sans: ['Inter','PingFang SC','Microsoft YaHei','system-ui','sans-serif'],
        mono: ['JetBrains Mono','ui-monospace','SFMono-Regular','Menlo','monospace'],
      },
    },
  },
  plugins: [],
}
