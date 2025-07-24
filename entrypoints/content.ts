export default defineContentScript({
	matches: ['<all_urls>'],
	main() {
		console.log('Hello content.');
	},
});

function getInputField() {
	const inputs = document.querySelectorAll('input'); //, textarea, select
	inputs.forEach((input) => {
		const labelElement = document.querySelector(`label[for="${input.id}"]`);
		const label = labelElement instanceof HTMLElement ? labelElement.innerText : input.placeholder || '';
		const value = fillInputField(label);
		value && (input.value = value);
	});
}

function fillInputField(label: string): string | null {
	label = label.toLowerCase();
	if (label.includes('name')) return 'John Doe';
	if (label.includes('email')) return 'john@example.com';
	if (label.includes('phone') || label.includes('number')) return '1234567890';
	if (label.includes('address')) return '123 Main St';
	if (label.includes('date')) return '2025-01-01';
	return null;
}
