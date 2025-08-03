import { defineConfig } from 'wxt';

// See https://wxt.dev/api/config.html
export default defineConfig({
	modules: ['@wxt-dev/module-react'],
	pages: {
		form: './src/pages/formPage.tsx', // 👈 This is your custom form page
	},
	manifest: {
		permissions: ['storage'],
	},
});
