import './App.css';

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
			<div className='card'>
				<button onClick={handleClick}>Autofill</button>
			</div>
		</>
	);
}

export default App;
