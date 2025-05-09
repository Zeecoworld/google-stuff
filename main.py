from flask import Flask, request, jsonify, send_from_directory
import logging
import traceback
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
from dataclasses import dataclass, field, asdict  # Added missing import for asdict
from typing import List, Optional, Dict, Any
import pandas as pd
import os
import csv
import random
import itertools

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)
logger = logging.getLogger(__name__)


@dataclass
class Business:
    """Holds business data"""
    name: str = None
    address: str = "No Address"
    website: str = "No Website"
    phone_number: str = "No Phone"
    reviews_count: int = 0
    reviews_average: float = 0.0
    latitude: float = None
    longitude: float = None


@dataclass
class BusinessList:
    """Holds list of Business objects and saves to both Excel and CSV."""
    business_list: list = field(default_factory=list)
    save_at: str = 'output'
    seen_businesses: set = field(default_factory=set)  # Set to track unique businesses

    def dataframe(self):
        """Transform business_list to a pandas dataframe."""
        return pd.json_normalize(
            (asdict(business) for business in self.business_list), sep="_"
        )

    def save_to_csv(self, filename, append=True):
        """Saves pandas dataframe to a single centralized CSV file with headers."""
        if not os.path.exists(self.save_at):
            os.makedirs(self.save_at)
        file_path = f"{self.save_at}/{filename}.csv"
        mode = 'a' if append else 'w'
        if append and os.path.exists(file_path):
            self.dataframe().to_csv(file_path, mode=mode, index=False, header=False)
        else:
            self.dataframe().to_csv(file_path, mode=mode, index=False, header=True)

    def add_business(self, business):
        """Add a business to the list if it's not a duplicate."""
        unique_key = (business.name, business.address, business.phone_number)
        if unique_key not in self.seen_businesses:
            self.seen_businesses.add(unique_key)
            self.business_list.append(business)
            return True  # Business was added
        else:
            return False  # Business was a duplicate


def extract_coordinates_from_url(url: str) -> tuple:
    """Helper function to extract coordinates from URL."""
    try:
        coordinates = url.split('/@')[-1].split('/')[0]
        return float(coordinates.split(',')[0]), float(coordinates.split(',')[1])
    except (IndexError, ValueError) as e:
        print(f"Error extracting coordinates: {e}")
        return None, None


def clean_business_name(name: str) -> str:
    """Remove '· Visited link' from the business name."""
    return name.replace(" · Visited link", "").strip() if name else "Unknown"


def get_cities_and_states_from_csv(filename):
    """Reads city and state data from a CSV file."""
    cities_states = []
    try:
        with open(filename, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if 'city' in row and 'state_id' in row:
                    cities_states.append((row['city'], row['state_id']))  # Append tuple (city, state)
    except FileNotFoundError:
        logger.error(f"Error: The file '{filename}' was not found. Please make sure it exists and the path is correct.")
        return []
    except Exception as e:
        logger.error(f"An error occurred while reading the CSV file: {e}")
        return []
    return cities_states


def select_random_city_and_state(cities_states):
    """Selects a random city and state from the provided list."""
    if not cities_states:
        return None, None  # Return None, None if the list is empty
    return random.choice(cities_states)


def spinning_cursor():
    """Generates a spinning cursor animation."""
    spinner = itertools.cycle(['|', '/', '-', '\\'])
    while True:
        yield next(spinner)


def scrape_google_maps(query, num_listings_to_capture, headless=True):
    """
    Scrapes Google Maps for a query, city, and state,
    handling errors and retries, and returns a list of Business objects.
    """
    listings_scraped = 0
    cities_states_original = get_cities_and_states_from_csv('uscities.csv')
    if not cities_states_original:
        logger.error("No cities/states loaded from CSV file")
        return []  # Return an empty list if no cities/states were loaded

    memory_list = BusinessList()
    spinner = spinning_cursor()

    # Configure Playwright for Docker environment
    playwright_config = {
        'chromium': {
            'headless': headless,
            'args': [
                '--disable-dev-shm-usage',  # Required for Docker
                '--no-sandbox',  # Required for Docker
                '--disable-setuid-sandbox',  # Required for Docker
                '--disable-gpu',  # Reduces resource usage
                '--disable-software-rasterizer',  # Reduces resource usage
            ]
        }
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(**playwright_config)
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        )
        page = context.new_page()

        while listings_scraped < num_listings_to_capture and len(cities_states_original) > 0:
            selected_city, selected_state = select_random_city_and_state(cities_states_original)
            if selected_city is None or selected_state is None:
                logger.info("No more cities to search.")
                break
                
            cities_states = cities_states_original.copy()
            cities_states.remove((selected_city, selected_state))
            logger.info(f"Searching for {query} in {selected_city}, {selected_state}.")
            search_for = f"{query} in {selected_city}, {selected_state}"

            try:
                page.goto("https://www.google.com/maps", timeout=30000)
                page.wait_for_selector('input#searchboxinput', timeout=10000)
                page.fill('input#searchboxinput', search_for)
                page.keyboard.press("Enter")
                
                # Wait for search results to load
                page.wait_for_selector('a[href^="https://www.google.com/maps/place"]', timeout=7000)
            except PlaywrightTimeoutError as e:
                logger.error(f"Timeout error occurred while searching for {query} in {selected_city}: {e}")
                continue  # Move to the next city
            except Exception as e:
                logger.error(f"Error occurred while searching for {query} in {selected_city}: {e}")
                continue  # Move to the next city

            try:
                current_count = page.locator('a[href^="https://www.google.com/maps/place"]').count()
            except Exception as e:
                logger.error(f"Error detecting results for {selected_city}, skipping: {e}")
                continue

            if current_count == 0:
                logger.info(f"No results found for {query} in {selected_city}, {selected_state}. Moving to next city.")
                continue

            logger.info(f"Found {current_count} listings for {query} in {selected_city}, {selected_state}.")

            MAX_SCROLL_ATTEMPTS = 10
            scroll_attempts = 0
            previously_counted = current_count

            while listings_scraped < num_listings_to_capture:
                try:
                    listings = page.locator('a[href^="https://www.google.com/maps/place"]').all()
                except Exception as e:
                    logger.error(f"Error while fetching listings: {e}")
                    break

                if not listings:
                    logger.info(f"No more listings found. Moving to the next city.")
                    break

                for listing in listings:
                    if listings_scraped >= num_listings_to_capture:
                        break

                    spinner_char = next(spinner)
                    logger.info(f"Scraping listing: {listings_scraped + 1} of {num_listings_to_capture} {spinner_char}")

                    MAX_CLICK_RETRIES = 5
                    clicked = False
                    for retry_attempt in range(MAX_CLICK_RETRIES):
                        try:
                            listing.click()
                            page.wait_for_timeout(2000)
                            clicked = True
                            break
                        except Exception as e:
                            logger.warning(f"Retrying click, attempt {retry_attempt + 1}: {e}")
                            page.wait_for_timeout(1000)
                    
                    if not clicked:
                        logger.warning("Failed to click on listing after multiple attempts, skipping...")
                        continue

                    # Extract business information
                    business = Business()

                    try:
                        # Get business name
                        business.name = clean_business_name(listing.get_attribute('aria-label'))
                        
                        # Get address
                        address_elem = page.locator('button[data-item-id="address"] div[class*="fontBodyMedium"]').first
                        if address_elem.count() > 0:
                            business.address = address_elem.inner_text()
                        
                        # Get website
                        website_elem = page.locator('a[data-item-id="authority"] div[class*="fontBodyMedium"]').first
                        if website_elem.count() > 0:
                            business.website = website_elem.inner_text()
                        
                        # Get phone number
                        phone_elem = page.locator('button[data-item-id^="phone:tel:"] div[class*="fontBodyMedium"]').first
                        if phone_elem.count() > 0:
                            business.phone_number = phone_elem.inner_text()
                        
                        # Extract reviews_average
                        reviews_avg_elem = page.locator('span[role="img"][aria-label*="stars"]').first
                        if reviews_avg_elem.count() > 0:
                            reviews_avg_text = reviews_avg_elem.get_attribute('aria-label')
                            if reviews_avg_text:
                                match = re.search(r'(\d+\.\d+|\d+)', reviews_avg_text.replace(',', '.'))
                                if match:
                                    business.reviews_average = float(match.group(1))
                        
                        # Extract reviews_count
                        reviews_count_elem = page.locator('button > span:has-text("reviews")').first
                        if reviews_count_elem.count() > 0:
                            reviews_count_text = reviews_count_elem.inner_text()
                            if reviews_count_text:
                                match = re.search(r'(\d+)', reviews_count_text.replace(',', ''))
                                if match:
                                    business.reviews_count = int(match.group(1))
                        
                        # Extract coordinates
                        business.latitude, business.longitude = extract_coordinates_from_url(page.url)
                        
                        added = memory_list.add_business(business)
                        if added:
                            listings_scraped += 1
                            logger.info(f"Added business: {business.name}")
                    except Exception as e:
                        logger.error(f"Error extracting business data: {e}")
                
                # Scroll down to load more results
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(3000)

                new_count = page.locator('a[href^="https://www.google.com/maps/place"]').count()
                if new_count == previously_counted:
                    scroll_attempts += 1
                    if scroll_attempts >= MAX_SCROLL_ATTEMPTS:
                        logger.info(f"No more listings found after {scroll_attempts} scroll attempts. Moving to next city.")
                        break
                else:
                    scroll_attempts = 0

                previously_counted = new_count

                if page.locator("text=You've reached the end of the list").is_visible():
                    logger.info(f"Reached the end of the list in {selected_city}, {selected_state}. Moving to the next city.")
                    break
        
        # Close browser
        browser.close()
        
    # Save results to CSV
    if memory_list.business_list:
        try:
            memory_list.save_to_csv(f"{query.replace(' ', '_')}_results", append=False)
        except Exception as e:
            logger.error(f"Error saving results to CSV: {e}")
    
    return memory_list.business_list


@app.route('/')
def index():
    """Serve the main HTML page"""
    return send_from_directory('templates', 'index.html')


@app.route('/api/scrape', methods=['POST'])
def scrape():
    """
    Endpoint to scrape Google Maps places based on a search query.
    """
    try:
        data = request.get_json()
        query = data.get('query')  # Get the query from the JSON payload
        num_listings_to_capture = int(data.get('num_listings', 20))  # default to 20
        if not query:
            return jsonify({"error": "Missing 'query' parameter"}), 400

        # Determine headless mode based on a parameter, default to True
        headless = bool(data.get('headless', True))

        logger.info(f"Starting scrape for query: {query}, listings: {num_listings_to_capture}, headless: {headless}")

        results = scrape_google_maps(query, num_listings_to_capture, headless)
        if not results:
            logger.warning(f"No places found for query: {query}")
            return jsonify({"error": f"No places found for query: {query}"}), 404
        
        # Convert dataclass objects to dictionaries for JSON serialization
        results_dict = [asdict(business) for business in results]
        return jsonify(results_dict), 200
    except Exception as e:
        error_message = f"An error occurred: {str(e)}"
        logger.exception(error_message)
        return jsonify({"error": error_message, "traceback": traceback.format_exc()}), 500

