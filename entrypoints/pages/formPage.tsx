import './App.css';
import React, { useState, useEffect } from 'react';

const fields = ['fname', 'lname', 'email', 'phone', 'linkedin', 'github', 'portfolio'];

function App() {
	const handleClick = () => {
		chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
			if (tabs[0]?.id) {
				chrome.tabs.sendMessage(tabs[0].id, { command: 'autofill' });
			}
		});
	};
	const [formData, setFormData] = useState<{ [key: string]: string }>({});
	useEffect(() => {
		chrome.storage.local.get(fields, (data) => {
			setFormData(data);
		});
	}, []);
	const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
		const { name, value } = e.target;
		const updated = { ...formData, [name]: value };
		setFormData(updated);
		chrome.storage.local.set({ [name]: value });
	};
	return (
		<>
			<form>
				{fields.map((field) => (
					<input
						key={field}
						name={field}
						placeholder={field.charAt(0).toUpperCase() + field.slice(1)}
						type='text'
						value={formData[field] || ''}
						onChange={handleChange}
					/>
				))}
			</form>
			<div className='card'>
				<button onClick={handleClick}>Autofill</button>
			</div>
		</>
	);
}

export default App;
