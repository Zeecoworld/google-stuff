from flask import Flask, request, jsonify,send_from_directory
import logging
import traceback
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
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
    """Holds list of Business objects."""
    business_list: list = field(default_factory=list)
    seen_businesses: set = field(default_factory=set)  # Set to track unique businesses

    # Removed dataframe method as it's only used for file saving
    # Removed save_to_csv method

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


def spinning_cursor():
    """Generates a spinning cursor animation."""
    spinner = itertools.cycle(['|', '/', '-', '\\'])
    while True:
        yield next(spinner)


def scrape_google_maps(query, num_listings_to_capture, headless=True):
    """
    Scrapes Google Maps for a given query,
    handling errors and retries, and returns a list of Business objects.
    headless: bool - Controls whether the browser runs in headless mode.
                     Defaults to True.
    """
    listings_scraped = 0
    memory_list = BusinessList()
    spinner = spinning_cursor()

    # Configure Playwright for Docker environment
    # The configuration for the specific browser (chromium) should be directly
    # passed as keyword arguments to the launch method.
    playwright_browser_config = {
        'headless': headless, # Use the passed headless value
        'args': [
            '--disable-dev-shm-usage',  # Required for Docker
            '--no-sandbox',  # Required for Docker
            '--disable-setuid-sandbox',  # Required for Docker
            '--disable-gpu',  # Reduces resource usage
            '--disable-software-rasterizer',  # Reduces resource usage
            # Add --disable-extensions and --disable-features if needed, but they are often included by default
        ]
    }

    # Define a limit for listings processed per scroll batch to reduce worker load
    MAX_LISTINGS_PER_SCROLL_BATCH = 10 # Reduced limit per scroll batch

    with sync_playwright() as p:
        browser = None # Initialize browser to None
        try:
            # Pass the configuration directly to p.chromium.launch
            browser = p.chromium.launch(**playwright_browser_config)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            )
            page = context.new_page()

            logger.info(f"Searching for: {query}.")
            search_for = query # Use the provided query directly

            try:
                # Increased initial page load timeout
                page.goto("https://www.google.com/maps", timeout=60000) # Increased timeout
                page.wait_for_selector('input#searchboxinput', timeout=20000) # Increased timeout
                page.fill('input#searchboxinput', search_for)
                page.keyboard.press("Enter")

                # Wait for search results to load with further increased timeout
                page.wait_for_selector('a[href^="https://www.google.com/maps/place"]', timeout=30000) # Increased timeout
            except PlaywrightTimeoutError as e:
                logger.error(f"Timeout error occurred while searching for {query}: {e}")
                return [] # Return empty list on failure
            except Exception as e:
                logger.error(f"Error occurred while searching for {query}: {e}")
                return [] # Return empty list on failure

            try:
                current_count = page.locator('a[href^="https://www.google.com/maps/place"]').count()
            except Exception as e:
                logger.error(f"Error detecting results for {query}, skipping: {e}")
                return []

            if current_count == 0:
                logger.info(f"No results found for {query}.")
                return []

            logger.info(f"Found {current_count} initial listings for {query}.")

            MAX_SCROLL_ATTEMPTS = 20 # Increased scroll attempts
            scroll_attempts = 0
            previously_counted = current_count

            while listings_scraped < num_listings_to_capture:
                try:
                    listings = page.locator('a[href^="https://www.google.com/maps/place"]').all()
                except Exception as e:
                    logger.error(f"Error while fetching listings: {e}")
                    break

                if not listings:
                    logger.info(f"No more listings found.")
                    break

                listings_processed_in_batch = 0 # Counter for listings processed in the current scroll batch

                for listing in listings:
                    if listings_scraped >= num_listings_to_capture:
                        break
                    if listings_processed_in_batch >= MAX_LISTINGS_PER_SCROLL_BATCH:
                        logger.info(f"Processed {MAX_LISTINGS_PER_SCROLL_BATCH} listings in this scroll batch. Scrolling to load more.")
                        break # Break the inner loop to scroll and get new listings

                    spinner_char = next(spinner)
                    logger.info(f"Scraping listing: {listings_scraped + 1} of {num_listings_to_capture} {spinner_char}")

                    MAX_CLICK_RETRIES = 5
                    clicked = False
                    for retry_attempt in range(MAX_CLICK_RETRIES):
                        try:
                            listing.click()
                            page.wait_for_timeout(1000) # Reduced wait after click slightly
                            clicked = True
                            break
                        except Exception as e:
                            logger.warning(f"Retrying click, attempt {retry_attempt + 1}: {e}")
                            page.wait_for_timeout(500) # Reduced wait on retry
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
                            listings_processed_in_batch += 1 # Increment batch counter
                            logger.info(f"Added business: {business.name}")

                        # Add a small delay after processing each listing
                        page.wait_for_timeout(500) # Wait for 500ms after processing a listing

                    except Exception as e:
                        logger.error(f"Error extracting business data: {e}")

                # Scroll down to load more results if we haven't reached the total
                if listings_scraped < num_listings_to_capture:
                    page.mouse.wheel(0, 5000)
                    page.wait_for_timeout(3000)

                    new_count = page.locator('a[href^="https://www.google.com/maps/place"]').count()
                    if new_count == previously_counted:
                        scroll_attempts += 1
                        if scroll_attempts >= MAX_SCROLL_ATTEMPTS:
                            logger.info(f"No more listings found after {scroll_attempts} scroll attempts.")
                            break
                    else:
                        scroll_attempts = 0

                    previously_counted = new_count

                    # Check for the "You've reached the end of the list" message
                    if page.locator("text=You've reached the end of the list").is_visible():
                         logger.info(f"Reached the end of the list for query: {query}.")
                         break

        except Exception as e:
            logger.error(f"An error occurred during Playwright operation: {e}")
            return []
        finally:
            # Ensure browser is closed even if errors occur
            if browser: # Check if browser object was successfully created
                browser.close()

    return memory_list.business_list


# Removed the index route as it's likely not needed without file output
@app.route('/')
def index():
    """Serve the main HTML page"""
    try:
        return send_from_directory('templates', 'index.html')
    except FileNotFoundError:
        return "Index page not found. Please ensure 'templates/index.html' exists.", 404


@app.route('/api/scrape', methods=['POST'])
def scrape():
    """
    Endpoint to scrape Google Maps places based on a search query.
    Forces headless mode to True for server environments.
    """
    try:
        data = request.get_json()
        query = data.get('query')  # Get the query from the JSON payload
        # Reduced default number of listings to capture
        num_listings_to_capture = int(data.get('num_listings', 10))  # default to 10
        if not query:
            return jsonify({"error": "Missing 'query' parameter"}), 400

        # Force headless mode to True for server environments
        headless = True

        logger.info(f"Starting scrape for query: '{query}', listings: {num_listings_to_capture}, headless: {headless}")

        results = scrape_google_maps(query, num_listings_to_capture, headless)
        if not results:
            logger.warning(f"No places found for query: '{query}'")
            return jsonify({"error": f"No places found for query: '{query}'"}), 404

        # Convert dataclass objects to dictionaries for JSON serialization
        results_dict = [asdict(business) for business in results]
        return jsonify(results_dict), 200
    except Exception as e:
        error_message = f"An error occurred: {str(e)}"
        logger.exception(error_message)
        return jsonify({"error": error_message, "traceback": traceback.format_exc()}), 500
