export default defineContentScript({
	matches: ['<all_urls>'],
	main() {
		console.log('Hello content.');
		chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
			if (message.command === 'autofill') {
				getInputField();
				sendResponse({ status: 'done' });
			}
		});
	},
});

function getInputField() {
	const inputs = document.querySelectorAll('form input'); //, textarea, select
	inputs.forEach((el) => {
		// const labelElement = document.querySelector(`span[for="${input.id}"]`);
		// const label = labelElement instanceof HTMLElement ? labelElement.innerText : input.placeholder || '';
		// console.log(input + input.placeholder + input.innerText);
		const input = el as HTMLInputElement;
		let label = '';
		const sibling = input.previousElementSibling;
		if (sibling instanceof HTMLElement && sibling.innerText.trim() !== '') {
			label = sibling.innerText.trim();
		} else {
			// Fallback to placeholder or empty
			label = input.placeholder || '';
		}

		// Debug: see what's happening
		console.log('Input:', input);
		console.log('Label/Placeholder:', label);

		const value = fillInputField(input.name);
		value && (input.value = value);
	});
}

function fillInputField(label: string): string | null {
	label = label.toLowerCase();
	if (label.includes('name')) return 'Krish Nalam';
	if (label.includes('email')) return 'nalamkrish1@gmail.com';
	if (label.includes('phone') || label.includes('number')) return '6474104439';
	if (label.includes('address')) return '107 Baycliffe Drive';
	if (label.includes('date')) return '2006-03-18';
	return label;
}
