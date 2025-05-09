from flask import Flask, request, jsonify,send_from_directory
import logging
import traceback
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import pandas as pd
import os
import csv
import random
import itertools

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
    return name.replace(" · Visited link", "").strip()


def get_cities_and_states_from_csv(filename):
    """Reads city and state data from a CSV file."""
    cities_states = []
    try:
        with open(filename, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                cities_states.append((row['city'], row['state_id']))  # Append tuple (city, state)
    except FileNotFoundError:
        print(f"Error: The file '{filename}' was not found.  Please make sure it exists and the path is correct.")
        return []
    except Exception as e:
        print(f"An error occurred while reading the CSV file: {e}")
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
        yield f"\033[91m{next(spinner)}\033[0m"  # Red-colored spinner using ANSI escape codes



def scrape_google_maps(query, num_listings_to_capture, headless=True):
    """
    Scrapes Google Maps for a  query, city, and state,
    handling errors and retries, and returns a list of Business objects.
    """
    listings_scraped = 0
    cities_states_original = get_cities_and_states_from_csv('uscities.csv')
    if not cities_states_original:
        return []  # Return an empty list if no cities/states were loaded

    memory_list = BusinessList()
    spinner = spinning_cursor()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        while listings_scraped < num_listings_to_capture and len(cities_states_original) > 0:
            selected_city, selected_state = select_random_city_and_state(cities_states_original)
            if selected_city is None or selected_state is None:
                print("No more cities to search.")
                break
            cities_states = cities_states_original.copy()
            cities_states.remove((selected_city, selected_state))
            print(f"Searching for {query} in {selected_city}, {selected_state}.")
            search_for = f"{query} in {selected_city}, {selected_state}"

            try:
                page.goto("https://www.google.com/maps", timeout=30000)
                page.wait_for_selector('//input[@id="searchboxinput"]', timeout=10000)
                page.locator('//input[@id="searchboxinput"]').fill(search_for)
                page.keyboard.press("Enter")
                page.wait_for_selector('//a[contains(@href, "https://www.google.com/maps/place")]',
                                        timeout=7000)
            except PlaywrightTimeoutError as e:
                print(
                    f"Timeout error occurred while searching for {query} in {selected_city}: {e}")
                continue  # Move to the next city
            except Exception as e:
                print(
                    f"Error occurred while searching for {query} in {selected_city}: {e}")
                continue  # Move to the next city

            try:
                current_count = page.locator('//a[contains(@href, "https://www.google.com/maps/place")]').count()
            except Exception as e:
                print(f"Error detecting results for {selected_city}, skipping: {e}")
                continue

            if current_count == 0:
                print(
                    f"No results found for {query} in {selected_city}, {selected_state}. Moving to next city.")
                continue

            print(f"Found {current_count} listings for {query} in {selected_city}, {selected_state}.")

            MAX_SCROLL_ATTEMPTS = 10
            scroll_attempts = 0
            previously_counted = current_count

            while listings_scraped < num_listings_to_capture:
                try:
                    listings = page.locator('//a[contains(@href, "https://www.google.com/maps/place")]').all()
                except Exception as e:
                    print(f"Error while fetching listings: {e}")
                    break

                if not listings:
                    print(f"No more listings found. Moving to the next city.")
                    break

                for listing in listings:
                    if listings_scraped >= num_listings_to_capture:
                        break

                    spinner_char = next(spinner)
                    print(
                        f"\rScraping listing: {listings_scraped + 1} of {num_listings_to_capture} {spinner_char}",
                        end='')

                    MAX_CLICK_RETRIES = 5
                    for retry_attempt in range(MAX_CLICK_RETRIES):
                        try:
                            listing.click()
                            page.wait_for_timeout(2000)
                            break
                        except Exception as e:
                            print(f"Retrying click, attempt {retry_attempt + 1}: {e}")
                            page.wait_for_timeout(1000)

                    name_attribute = 'aria-label'
                    address_xpath = 'xpath=//button[@data-item-id="address"]//div[contains(@class, "fontBodyMedium")]'
                    website_xpath = 'xpath=//a[@data-item-id="authority"]//div[contains(@class, "fontBodyMedium")]'
                    phone_number_xpath = 'xpath=//button[contains(@data-item-id, "phone:tel:")]//div[contains(@class, "fontBodyMedium")]'

                    # Define the details panel to scope our locators
                    details_panel = page.locator('div[role="main"]')

                    business = Business()

                    business.name = clean_business_name(
                        listing.get_attribute(name_attribute)) if listing.get_attribute(
                        name_attribute) else "Unknown"
                    business.address = page.locator(address_xpath).first.inner_text() if page.locator(
                        address_xpath).count() > 0 else "No Address"
                    business.website = page.locator(website_xpath).first.inner_text() if page.locator(
                        website_xpath).count() > 0 else "No Website"
                    business.phone_number = page.locator(phone_number_xpath).first.inner_text() if page.locator(
                        phone_number_xpath).count() > 0 else "No Phone"

                    # Extract reviews_average
                    reviews_average_element = details_panel.locator(
                        'xpath=.//span[@role="img" and @aria-label and contains(@aria-label, "stars")]').first
                    if reviews_average_element.count() > 0:
                        reviews_average_text = reviews_average_element.get_attribute('aria-label')
                        if reviews_average_text:
                            match = re.search(r'(\d+\.\d+|\d+)', reviews_average_text.replace(',', '.'))
                            if match:
                                business.reviews_average = float(match.group(1))
                            else:
                                business.reviews_average = 0.0
                        else:
                            business.reviews_average = 0.0
                    else:
                        business.reviews_average = 0.0

                    # Extract reviews_count
                    reviews_count_element = details_panel.locator(
                        'xpath=.//button[./span[contains(text(), "reviews")]]/span').first
                    if reviews_count_element.count() > 0:
                        reviews_count_text = reviews_count_element.inner_text()
                        if reviews_count_text:
                            match = re.search(r'(\d+)', reviews_count_text.replace(',', ''))
                            if match:
                                business.reviews_count = int(match.group(1))
                            else:
                                business.reviews_count = 0
                        else:
                            business.reviews_count = 0
                    else:
                        business.reviews_count = 0

                    business.latitude, business.longitude = extract_coordinates_from_url(page.url)

                    added = memory_list.add_business(business)
                    if added:
                        listings_scraped += 1

                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(3000)

                new_count = page.locator('//a[contains(@href, "https://www.google.com/maps/place")]').count()
                if new_count == previously_counted:
                    scroll_attempts += 1
                    if scroll_attempts >= MAX_SCROLL_ATTEMPTS:
                        print(
                            f"No more listings found after {scroll_attempts} scroll attempts. Moving to next city.")
                        break
                else:
                    scroll_attempts = 0

                previously_counted = new_count

                if page.locator("text=You've reached the end of the list").is_visible():
                    print(
                        f"Reached the end of the list in {selected_city}, {selected_state}. Moving to the next city.")
                    break
            browser.close()
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
        num_listings_to_capture = data.get('num_listings', 20) #default to 20
        if not query:
            return jsonify({"error": "Missing 'query' parameter"}), 400

        # Determine headless mode based on a parameter, default to True
        headless = data.get('headless', True)



        results = scrape_google_maps(query, num_listings_to_capture, headless)
        if not results:
            return jsonify({"error": f"No places found for query: {query}"}), 404
        return jsonify(results), 200
    except Exception as e:
        error_message = f"An error occurred: {str(e)}"
        logger.exception(error_message)
        return jsonify({"error": error_message}), 500  # Return 500 for server error



# if __name__ == '__main__':
#     app.run(debug=True, port=5000)
