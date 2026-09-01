// Application bootstrap: createApp + Pinia + Vue Router + vue-i18n + Element Plus.
// Element Plus is registered globally; auto-import for icons comes in T6.2.

import { createApp } from 'vue';
import { createPinia } from 'pinia';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';

import App from './App.vue';
import { router } from './router';
import { i18n } from './i18n';

const app = createApp(App);

app.use(createPinia());
app.use(router);
app.use(i18n);
app.use(ElementPlus);

app.mount('#app');