import { createApp, h } from 'vue'
import {
  ElAlert, ElButton, ElCheckbox, ElConfigProvider, ElDialog, ElDrawer, ElForm, ElFormItem,
  ElIcon, ElInput, ElLoading, ElOption, ElSelect, ElSwitch, ElTable, ElTableColumn, ElTag,
} from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import App from './App.vue'
import './style.css'
import './motion.css'

const app = createApp({ render: () => h(ElConfigProvider, { locale: zhCn }, () => h(App)) })
for (const component of [
  ElAlert, ElButton, ElCheckbox, ElDialog, ElDrawer, ElForm, ElFormItem, ElIcon, ElInput,
  ElOption, ElSelect, ElSwitch, ElTable, ElTableColumn, ElTag,
]) {
  app.use(component)
}
app.use(ElLoading)
app.mount('#app')

