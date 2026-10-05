from database.database import SessionLocal
from database.models import Restaurant


def add_restaurant():
    db = SessionLocal()

    try:
        restaurant = Restaurant(
            name="Riwayat tabaq _ رواية طبق",
            instagram_username="riwayettabaq.res",
            instagram_url="https://www.instagram.com/riwayettabaq.res",
            email="sample-user-1@example.test",
            location="Al Bahah",
        )

        db.add(restaurant)
        db.commit()
        db.refresh(restaurant)

        print("Restaurant added successfully.")
        print(f"Restaurant ID: {restaurant.id}")
        print(f"Restaurant: {restaurant.name}")

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":
    add_restaurant()