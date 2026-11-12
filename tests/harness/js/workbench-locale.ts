import { setLocaleResources } from '../../../src/clients/shared/core/i18n/index.js';
import chinese from '../../../src/clients/shared/core/i18n/messages/zh-cn.json';

// 进程内组件测试没有产品 bootstrap；显式建立同一默认语言，不为生产入口增加回退。
setLocaleResources('zh-cn', chinese);
