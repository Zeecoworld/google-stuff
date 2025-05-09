from flask import Flask, request, jsonify, send_from_directory
import logging
import traceback
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
from dataclasses import dataclass, field, asdict
import os
import signal
from functools import wraps
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)
logger = logging.getLogger(__name__)

# Increased default timeout for the route (in seconds)
DEFAULT_TIMEOUT = 60

# Add timeout decorator to prevent long-running operations
def timeout_handler(signum, frame):
    raise TimeoutError("Operation timed out")

def timeout_decorator(timeout_duration):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise TimeoutError(f"Function {func.__name__} timed out after {timeout_duration} seconds")

            # Set the timeout handler
            # Check if we are in the main thread before setting the alarm
            # signal.SIGALRM is only available in the main thread
            if os.getpid() == os.getppid(): # Simple check if likely in main process (not always accurate in complex setups)
                 original_handler = signal.getsignal(signal.SIGALRM)
                 signal.signal(signal.SIGALRM, handler)
                 signal.alarm(timeout_duration)

            try:
                result = func(*args, **kwargs)
            finally:
                # Reset the alarm and restore original handler
                if os.getpid() == os.getppid():
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, original_handler)
            return result
        return wrapper
    return decorator


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
        logger.warning(f"Error extracting coordinates: {e}")
        return None, None


def clean_business_name(name: str) -> str:
    """Remove '· Visited link' from the business name."""
    return name.replace(" · Visited link", "").strip() if name else "Unknown"


def scrape_google_maps(query, num_listings_to_capture=5, timeout=55): # Adjusted internal timeout
    """
    Scrapes Google Maps for a given query, with strict limits.
    Returns a list of Business objects.

    Args:
        query: Search query
        num_listings_to_capture: Maximum number of listings to capture (default: 5)
        timeout: Maximum time allowed for scraping in seconds (default: 55) - Should be less than route timeout
    """
    # Strictly limit the number of listings to prevent overload
    num_listings_to_capture = min(num_listings_to_capture, 5)

    listings_scraped = 0
    memory_list = BusinessList()

    # Extremely conservative browser configuration for server environments
    playwright_browser_config = {
        'headless': True,
        'args': [
            '--disable-dev-shm-usage',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-extensions',
            '--disable-features=TranslateUI',
            '--disable-component-extensions-with-background-pages',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-ipc-flooding-protection',
            '--single-process',  # Critical for reduced memory usage
            '--memory-pressure-off',
            '--mute-audio',
            '--disable-default-apps',
            '--no-default-browser-check',
            '--disable-client-side-phishing-detection',
            '--disable-sync'
        ]
    }

    # Set a time boundary for the function
    start_time = time.time()

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(**playwright_browser_config)
            context = browser.new_context(
                viewport={'width': 800, 'height': 600},  # Reduced viewport size
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                java_script_enabled=True,  # Ensure JavaScript is enabled
                bypass_csp=True,  # Bypass Content Security Policy
                ignore_https_errors=True  # Ignore HTTPS errors
            )

            # Reduce memory by limiting pages to 1
            page = context.new_page()
            page.set_default_timeout(10000)  # Increased default Playwright operation timeout to 10 seconds

            logger.info(f"Searching for: {query}")

            try:
                # Increased initial page load timeout
                page.goto("https://www.google.com/maps", timeout=45000) # Still relatively high
                page.wait_for_selector('input#searchboxinput', timeout=15000) # Still relatively high
                page.fill('input#searchboxinput', query)
                page.keyboard.press("Enter")

                # Wait for search results to load with increased timeout, less than route timeout
                page.wait_for_selector('a[href^="https://www.google.com/maps/place"]', timeout=50000) # Increased to 50 seconds
            except PlaywrightTimeoutError as e:
                logger.error(f"Timeout error occurred while searching for {query}: {e}")
                return []
            except Exception as e:
                logger.error(f"Error occurred while searching for {query}: {e}")
                return []

            try:
                # Get the current count of listings
                current_count = page.locator('a[href^="https://www.google.com/maps/place"]').count()
                logger.info(f"Found {current_count} initial listings for {query}")

                if current_count == 0:
                    logger.info(f"No results found for {query}")
                    return []

                # Process listings with strict time boundary checking
                # Only attempt to get the first 'num_listings_to_capture' elements
                listings = page.locator('a[href^="https://www.google.com/maps/place"]').all()[:num_listings_to_capture]

                for listing in listings:
                    # Check if we've exceeded our time limit for scrape_google_maps
                    if time.time() - start_time > timeout:
                        logger.warning(f"Scraping operation within scrape_google_maps timed out after {timeout} seconds")
                        break

                    if listings_scraped >= num_listings_to_capture:
                        break

                    try:
                        listing.click()
                        page.wait_for_timeout(800)  # Reduced wait time

                        # Extract business information
                        business = Business()

                        # Get business name
                        business.name = clean_business_name(listing.get_attribute('aria-label'))

                        # Address
                        address_elem = page.locator('button[data-item-id="address"] div[class*="fontBodyMedium"]').first
                        if address_elem.count() > 0:
                            business.address = address_elem.inner_text()

                        # Website
                        website_elem = page.locator('a[data-item-id="authority"] div[class*="fontBodyMedium"]').first
                        if website_elem.count() > 0:
                            business.website = website_elem.inner_text()

                        # Phone number
                        phone_elem = page.locator('button[data-item-id^="phone:tel:"] div[class*="fontBodyMedium"]').first
                        if phone_elem.count() > 0:
                            business.phone_number = phone_elem.inner_text()

                        # Reviews average
                        reviews_avg_elem = page.locator('span[role="img"][aria-label*="stars"]').first
                        if reviews_avg_elem.count() > 0:
                            reviews_avg_text = reviews_avg_elem.get_attribute('aria-label')
                            if reviews_avg_text:
                                match = re.search(r'(\d+\.\d+|\d+)', reviews_avg_text.replace(',', '.'))
                                if match:
                                    business.reviews_average = float(match.group(1))

                        # Reviews count
                        reviews_count_elem = page.locator('button > span:has-text("reviews")').first
                        if reviews_count_elem.count() > 0:
                            reviews_count_text = reviews_count_elem.inner_text()
                            if reviews_count_text:
                                match = re.search(r'(\d+)', reviews_count_text.replace(',', ''))
                                if match:
                                    business.reviews_count = int(match.group(1))

                        # Coordinates
                        business.latitude, business.longitude = extract_coordinates_from_url(page.url)

                        added = memory_list.add_business(business)
                        if added:
                            listings_scraped += 1
                            logger.info(f"Added business: {business.name}")

                        # Add a small delay after processing each listing
                        page.wait_for_timeout(300) # Reduced wait time further

                    except Exception as e:
                        logger.warning(f"Error processing a listing: {e}")
                        continue

                # No scrolling loop needed with strict listing limit

            except Exception as e:
                logger.error(f"Error scraping listings: {e}")

        except Exception as e:
            logger.error(f"An error occurred during Playwright operation: {e}")
            return []
        finally:
            # Ensure browser is properly closed to free memory
            if browser:
                browser.close()

    return memory_list.business_list


@app.route('/')
def index():
    """Serve the main HTML page"""
    try:
        return send_from_directory('templates', 'index.html')
    except FileNotFoundError:
        return "Index page not found. Please ensure 'templates/index.html' exists.", 404
        


@app.route('/api/scrape', methods=['POST'])
@timeout_decorator(DEFAULT_TIMEOUT) # Apply the increased timeout decorator
def scrape():
    """
    Endpoint to scrape Google Maps places with strict resource limits.
    Forces headless mode to True for server environments.
    """
    try:
        data = request.get_json()
        query = data.get('query')

        if not query:
            return jsonify({"error": "Missing 'query' parameter"}), 400

        # Strictly limit number of listings to 5
        num_listings_to_capture = min(int(data.get('num_listings', 3)), 5)
        logger.info(f"Starting scrape for query: '{query}', limited to {num_listings_to_capture} listings")

        # Call scrape_google_maps with an internal timeout less than the route timeout
        results = scrape_google_maps(query, num_listings_to_capture, timeout=DEFAULT_TIMEOUT - 5) # Set internal timeout 5 seconds less

        if not results:
            return jsonify({"message": "No results found or timeout occurred", "results": []}), 200

        # Convert dataclass objects to dictionaries for JSON serialization
        results_dict = [asdict(business) for business in results]
        return jsonify({"message": f"Found {len(results_dict)} results", "results": results_dict}), 200

    except TimeoutError as e:
        logger.warning(f"Request timed out: {str(e)}")
        # Return a 503 Service Unavailable or 408 Request Timeout
        return jsonify({"error": "Request timed out. The scraping operation took too long."}), 503
    except Exception as e:
        error_message = f"An error occurred: {str(e)}"
        logger.exception(error_message)
        return jsonify({"error": error_message}), 500
