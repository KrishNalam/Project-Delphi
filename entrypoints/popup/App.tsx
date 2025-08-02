import './App.css';
import React, { useState, useEffect } from 'react';

type UserData = {
	name: string;
	email: string;
	phone: string;
	address: string;
	linkedin: string;
	github: string;
	portfolio: string;
};

const defaultUserData: UserData = {
	name: '',
	email: '',
	phone: '',
	address: '',
	linkedin: '',
	github: '',
	portfolio: '',
};

function App() {
	const handleClick = () => {
		chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
			if (tabs[0]?.id) {
				chrome.tabs.sendMessage(tabs[0].id, { command: 'autofill' });
			}
		});
	};

	return (
		<>
			<form>
				<input placeholder='First Name' type='text' id='fname' />
				<input placeholder='Last Name' type='text' id='lname' />
				<input placeholder='Email' type='text' id='email' />
				<input placeholder='Phone' type='text' id='phone' />
				<input placeholder='LinkedIn' type='text' id='linkedin' />
				<input placeholder='GitHub' type='text' id='github' />
				<input placeholder='Portfolio' type='text' id='portfolio' />
			</form>
			<div className='card'>
				<button onClick={handleClick}>Autofill</button>
			</div>
		</>
	);
}

export default App;
